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
| **Committing while the suite runs** | two tests compare live `git` state against import-time `__version__`/`__build__`; a commit mid-run moved both and reported two false failures | let a run finish before touching the tree; re-run before believing a red |

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

---

## 16. The SPY comparison restarted itself — three causes, one mistake

Owner-reported 2026-09-12: *"something has gone wrong, it has reset my
SPY track, it has randomly gone to tracking from 11/09 when it started
14/08, figure out why?"*

**It was real, it was mine, and it fired three separate ways.** All three
are the same mistake in different clothes: **absence of evidence read as
evidence that the broker account had changed.** `sync_with_account`'s own
docstring has always promised *"AN OWNER-SET BASELINE IS NOT
OVERWRITTEN"*, and the code did the opposite on the very next cycle.

| # | cause | measured | why it fired |
|---|---|---|---|
| 1 | **An owner-set baseline stores no fingerprint** | seeded `owner_set`, `account_fingerprint=""`, ran one healthy cycle: `changed=True`, `source=account_changed`, `start_date` moved to today | the setup form cannot know the broker's account id, so `""` != the live hash and the mismatch branch fired. The reason it wrote claimed the account had changed **"fingerprint  -> 5a8297…"** — from a blank to a real one, which is not a change of account, it is the first time anyone looked |
| 2 | **The fingerprint was `id or account_number`** | two reads of the SAME account, the second omitting `id`: `changed=True` | one read that happened to omit a field hashed the *other* field, matched nothing, and looked like a different account. A stored value that depends on which fields a particular read carried is a value that flips |
| 3 | **An unreadable row reported `source="unset"`** | one `UPDATE … SET start_date='not-a-date'`: `changed=True`, a second row written, `start_date` = today | `is_placeholder` is `source == "unset"`, which `sync_with_account` reads as *"nothing has ever been stored"* — so **one row it could not parse** struck a fresh baseline at today and discarded a month of tracking |

Cause 1 is the one that matches the owner's report exactly: they set the
baseline by hand, and the next fifteen-minute cycle overwrote it.

### What changed: a restart now needs POSITIVE evidence

Three explicit branches, each returning `(now, False)` and writing
nothing:

| state | old behaviour | now |
|---|---|---|
| the baseline could not be read | struck a new one at today | `is_unreadable` is its own fact, distinct from `is_placeholder`; never replaced, and the scheduler logs it at **ERROR** saying it has been "LEFT EXACTLY AS IT IS" |
| the read carries no canonical `id` | mismatch → restart | inconclusive → nothing. "Different account" and "same account reported by a different field" are indistinguishable, and one answer destroys a month of tracking while the other costs nothing |
| the baseline carries no fingerprint | mismatch → restart | the fingerprint is **adopted onto it**, keeping the owner's own capital and start date, with `NOT restarted` in the reason |

**Compare wide, write narrow.** `account_fingerprints()` hashes every
identifier the payload carries and the match is an **intersection**, so a
payload missing a field is still recognised and rows written by the older
`id or account_number` code keep matching. The stored value is always
`id` (`CANONICAL_ID_FIELD`), so it cannot depend on which fields a read
happened to include.

**TRIED AND BACKED OUT: storing the full SET of fingerprints.** The first
version accumulated every identifier ever seen into the column. It works,
and it **appends a row every time a read presents a field the baseline has
not recorded yet** — which broke the existing property that a settled
baseline writes no second row, and would have filled the owner's history
with near-identical rows. Wide comparison plus a narrow canonical write
gets the same recognition with no churn.

### The property that must not break, and does not

The owner's own instruction is *"when I change the Alpaca keys i want it
to register there is a new account and restart the SPY tracker"*. Checked
by running it: a genuinely different `id` still restarts —
`source=account_changed`, `start_date` = today, capital struck from the
new account's equity, and the previous baseline still in the history.

### The owner's 14/08 baseline is recoverable

`benchmark_baselines` is **append-only** — that design decision is what
saved this. Every baseline the bot ever struck, including the owner's
original, is still in the table with its capital, its date and the reason
it was replaced. Re-entering it on the performance page now sticks,
because cause 1 was what ate it.

### Verification

- **12 sabotage breakages, all 12 caught red**, each verified to still
  import first. The first round reported one "not caught" that was
  actually a **sabotage that never applied** — the target string is split
  across two source lines, so `count(old)` was 0. The script asserts
  every replacement applied (§6, "a multi-edit script that writes
  nothing"), which is the only reason that was visible rather than being
  recorded as a passing sabotage.
- Four existing tests in `test_benchmark_baseline.py` **encoded the bug**
  and were inverted, not deleted — e.g.
  `test_unreadable_capital_cents_degrades_to_a_placeholder` became
  `test_unreadable_capital_cents_is_unreadable_not_absent`. A test
  asserting the broken behaviour is why this shipped.
- Full suite green offline: **3871 tests**.

---

## 17. A Google font loader where the explanation belongs

Owner-reported 2026-09-12, with a screenshot of the Pipeline page:

```
NEEDS ATTENTION
FEEDS THAT COULD NOT BE READ
1  Insider trades (SEC Form 4) could not be read
   WebFontConfig = { google: { families: [ 'Raleway:300,400,500,600:latin' ] } };
   (function() { var wf = document.createElement('script');
   wf.src = '//ajax.googleapis.com/ajax/libs/webfont/1/webfont.js'; ...
```

listed **twice**, with "1" beside each.

### Reproduced character for character, offline, before anything changed

```
>>> _fault_gist(a_sec_error_page)
"SEC.gov | Site Temporarily Unavailable WebFontConfig = { google: {
 families: [ 'Raleway:300,400,500,600:latin' ] } }; (function() { var wf =
 document.createElement('script'); wf.src = '//ajax.googleap..."
```

**This was guaranteed, not unlucky.** `_fault_gist` stripped HTML *tags*
with `re.sub(r"<[^>]+>", " ", text)`, which leaves the **contents** of a
`<script>` untouched — and sec.gov puts its Google WebFont loader at the
very top of `<head>`, ahead of any prose. So for **any** sec.gov page not
in a four-entry marker table, the "one readable sentence" was that
loader. Every time.

### Four defects, and the second one is why no dashboard work could fix it

| # | defect | measured |
|---|---|---|
| 1 | tags stripped, `<script>`/`<style>` **contents** kept | the reproduction above |
| 2 | **`cycle.py` recorded `exc.raw_text` ALONE** | the exception was carrying `message="HTTP 503 after 4 attempts"`, `status_code=503`, the URL and `attempts=4`. All four were discarded **at the point of writing**, so the database never had them. `FeedError.__str__` formats exactly that summary and **nothing had ever called it** |
| 3 | a `RateLimitBlocked` wrote **no row at all** — it logged and returned `[]` | so the panel's own **first** fault sentence, the one for a rate-limit block, was **unreachable for this feed**. A block was visible only to somebody reading the log file, which is the SSH-free troubleshooting the brief rules out |
| 4 | the count beside each row was the literal `1` | "once" and "forty times" rendered identically, and the owner's two rows each claimed 1 |

### What changed

**House rule 7 throughout: classify by the rule.** An HTML **document**
where an `.idx` file or a filing's text was expected is an upstream error
page, *whatever it says* — so it now reads *"the server returned a web
page instead of data (the page is titled "SEC.gov | Site Temporarily
Unavailable") — so this is an error or maintenance page at the source,
not a problem at this end."* The marker table is only for pages whose
sentence should say what to **do** (the rate-limit block, an undeclared
User-Agent, an unpublished index).

Machinery elements are removed **contents and all**, including an
unclosed one, so no JavaScript or CSS can ever be presented as prose.

The recorded `error_text` now carries **the diagnosis first, the verbatim
body after it**, separated by a marker, in the same column — no schema
change, and a row written before this still reads. **House rule 3 is
unchanged**: the raw response is still kept in full; it moves *below* a
sentence instead of *being* the sentence. The fold labelled "the exact
response from the server" shows only the server's half, because putting
our own summary in it labels our words as theirs.

Faults are grouped on `sources.fault_key` — the exception type and
message **without** the per-attempt facts, because the URL carries the
day's date and keying on the whole line would put each day's identical
outage in its own group and count 1 forever. And *"N feed(s) failed to
read"* counts **sources**: one feed failing forty times is one feed, and
saying "40 feeds failed" sends the reader hunting for thirty-nine that do
not exist.

### The generic-word trap, found while writing the rule

`"timeout"` was one of the four markers and was matched against the
**raw** body, so a maintenance page whose own prose happened to use the
word reported as a network timeout — which sends the reader to look at
the wrong thing entirely. Page **identities** (distinctive phrases that
appear only in the page they name) are now checked first, the HTML rule
second, and ordinary English words last.

### My own fix had a defect, and my own test caught it

The first version decided "is this a body or a summary?" by **"does it
look like a whole HTML document"**. A markup **fragment** — a truncated
page, or a body starting mid-document — has no `<html>`, fell to the
summary branch, and was printed as prose *markup and all*. Found by this
module's own test failing, not by reasoning about it. The rule is now
simply: **no marker means no recorded diagnosis, so the whole thing is
the body** — which handles a document, a fragment and plain text alike.

### Verification

- **22 sabotage breakages, 21 caught red**, each verified to still import
  first.
- **Two of my tests could not fail**, both found by sabotage and both the
  same cause: they used a full `<html>` document, so the HTML-document
  rule returned early and `_visible_text` — the thing under test — was
  never reached. Rewritten against fragments, and a third test added that
  distinguishes the two paths by **behaviour** (an `AccessDenied`
  fragment must produce the actionable "not published" sentence, which
  only the body path can give it) rather than by both merely coming out
  clean.
- **One sabotage stays green alone and is recorded as such** in the code:
  sanitising the summary half has no reachable input today, because the
  head is only ever text this project's own writer produced. Its pair
  covers it; breaking **both** goes red. Same as §14's pair — the right
  way to show defence in depth is load-bearing.
- **A process lesson, cost about ten minutes:** re-running the sabotage
  script piped to `head` killed it with SIGPIPE **mid-round**, leaving a
  sabotage applied in the working tree. The next full-suite run failed
  for a reason unrelated to the code. A sabotage harness that restores in
  a `finally` would have survived it; piping its output to `head` is the
  thing not to do.
- Full suite green offline: **3904 tests**.

### What is NOT claimed

**Which sec.gov page the owner actually hit is unknown**, and this change
does not need to know. The recorded row now names the status code, the
URL and the attempt count, so the *next* occurrence answers that
question from the dashboard — which is the point: the diagnosis was
never stored, so it could not be read out afterwards no matter how the
panel was written.

---

## 18. Ten stocks beside the bot, and the arithmetic that says colour cannot carry them

Owner-asked 2026-09-12: *"on the tab where i can set where to track SPY
from, can you edit it a bit so i can track up to 10 stocks at once, I
type the stock name exactly and set the date and amount, set SPY as
default, but then show as different colours on the graph so I can track
how we are beating multiple stocks."*

### Measured first: eleven lines cannot be told apart by colour

Before choosing anything, the palette was computed — CIE76 ΔE against
this dashboard's own two surfaces, under normal vision plus simulated
deuteranopia, protanopia and tritanopia, greedily ordered so the first
*n* slots are the best-separated *n*:

| series drawn | worst-pair CVD ΔE, light / dark |
|---|---|
| 2 | 96.3 / 95.7 |
| 3 | 42.8 / 42.6 |
| 4 | 18.6 / 26.5 |
| **5** | **14.9 / 18.4** — the last row that is reliable |
| 6 | 14.0 / 11.1 |
| 8 | 11.7 / 7.1 |
| 11 | **5.8 / 4.0** |

A first attempt with hand-picked hues measured **0.6** on its worst pair
(the bot's blue against an indigo, identical under deuteranopia), which
is what made the point unarguable.

**So colour is a grouping cue and never the identifier.** Each line also
carries its own dash pattern, and its **ticker is printed at the
right-hand end of its own line** — which is strictly better than a
legend anyway, because reading a legend requires exactly the hue
discrimination the table above says a reader does not have. That is this
project's standing rule (a status is never carried by colour alone)
applied to the case where the arithmetic says it cannot be. Every stroke
also clears 2.4:1 against its own surface: a line nobody can see is
worse than one they confuse.

`slate` was computed into slot 2 and **removed by hand** — it is
maximally distant precisely because it is neutral, and a grey line among
coloured ones reads as chrome. The cost of dropping it was 42.8 instead
of 45.3 at three series.

### Four things the feature needed that were not the feature

| # | what | why it mattered |
|---|---|---|
| 1 | **A separate table**, not a column on `benchmark_baselines` | that table is append-only and `benchmark.current()` reads its newest row as **the account baseline**. A per-ticker row in it would be returned as the account's own comparison — and §16 is this same baseline being reset under the owner three different ways. A test asserts the baseline table has no ticker column |
| 2 | **SPY had to survive the first add** | the list is *synthesised* from the account baseline while empty, so the first real write would have made the new stock the only row and silently dropped the line the owner was reading. `seed_from_baseline` writes SPY first |
| 3 | **Per-symbol cache metadata** | `BarCache` wrote **one** `cache_meta.json` for the whole root, so refreshing AAPL would have overwritten SPY's `feed` pin — and SPY's next refresh would then append one exchange's prints to a consolidated-tape series. That is the exact "never mix bases" failure `catalyst/data/benchmark.py` exists to prevent, and a silent one. SPY keeps the unqualified file it has always had; everything else gets its own |
| 4 | **Something had to fetch the bars** | `refresh_benchmark` was hard-wired to SPY, so every new line would have been permanently empty. It takes a symbol now, with `refresh_comparisons` looping, its **own** daily marker so a mistyped ticker cannot hold SPY's refresh up, and the marker set only when nothing is stuck — the defect that once left the SPY line 48 hours stale |

**Number 3 was found by reading `BarCache` before using it, not by a
test.** That is the fifth time in this project that the thing which
broke a change was one level away from it.

### The default is synthesised, not seeded

With no stored rows the list *is* SPY bought with the account baseline's
own money on its own date — byte for byte what this page drew before the
list existed. So an owner who never opens the form sees no change, no
migration writes anything, and the list can never be empty (a page built
to compare cannot do it with one line). Removing every row falls back to
the same default.

### My own fix had a defect, and my own test caught it

The end-label stack was slid **up** by its bottom overflow. With eleven
lines crammed against the floor — ten stocks in a drawdown, which is a
real Tuesday — that pushed the topmost label clean out through the **top**
of the chart:

```
labels outside the viewBox: ["'BOT' box=(776.5,-75.3,797.2,-61.0)"]
```

Written for the bottom case; the top was never considered. It now fits on
both sides, and falls back to even spacing when the labels cannot both
follow their own lines and stay inside the plot.

**And a second one of the same kind:** the label gap was `FONT_SIZE + 2`
= 13 against `text_boxes`' own measured box height of 14.3 — so the
project's own measurement tool reported an overlap the code believed it
had prevented. Two numbers meaning the same thing, quietly disagreeing
(§6's "two constants that later became equal", in reverse). Both now
derive from one `LINE_H`.

**A third, found by a test that had nothing to do with this feature.**
`BarCache.load_bars` raises a message written for a developer running a
backtest — *"run scripts/fetch_history.py first; the backtest never
fetches mid-run"* — and the brief says the owner must never be told to
run a script. So I replaced it with a helpful paragraph, and
`test_the_overview_is_no_longer_mostly_prose` went red:

```
assert (1578 / 21) < 75      # the page is still an essay with numbers in it
```

Measured, the performance panel had grown by **59 words**, and a word-by-
word diff named them: the whole paragraph, appearing on the **account's
own SPY** — where *"check SPY is spelled exactly right"* is nonsense,
because SPY is not a ticker the owner typed. **The advice belonged to the
display, not to the loader.** The loader now states the fact in eight
words with the raw exception after it, and `panels._tracked_stocks` adds
the wait-or-check-the-spelling sentence for the stocks the owner actually
typed.

Worth recording because the word budget caught a **correctness** bug, not
a style one: a guardrail against prose found a sentence being shown to
the wrong reader.

**A fourth, and it is the third one the same wrong reader.** Rendering a
**brand-new install** — no baseline, no bars — showed the synthesised SPY
default row saying *"the ticker is probably not one Alpaca knows - check
the spelling."* The owner never typed SPY. Exactly the sentence just moved
out of the loader for being shown to the account's own benchmark, back
again one level up for the default row. The advice is now gated on the row
NOT being the default, and the default gets its own shorter line saying
when its line will appear.

**The lesson, having now paid for it three times in one change:** a
sentence that says "you typed this" must be gated on the reader having
typed it, and the only reliable way to find where it leaks is to render
every state the page has — including the empty one. Found by rendering a
fresh install, not by a test.

### Recurring failure, and it was mine twice in one session

**Killing a sabotage harness mid-run leaves the sabotage in the working
tree.** §17 recorded it once (piping the script to `head`, SIGPIPE). I
then did it again — `pkill` while a round was live, whose `finally` never
ran — and `FIRST_COMPARISON_SLOT = 0` sat in the tree until a test
failed for a reason unrelated to what it tests. The second harness *did*
restore in a `finally`; a signal does not run one.

The check that actually works is not a better harness, it is **auditing
every sabotage target afterwards**: a script asserting each target string
is present at its expected count, run before trusting any suite result.
That found the one left-over immediately and confirmed the other thirty-
eight were clean.

### Verification

- **46 sabotage breakages, 44 caught red**, each verified to still
  import first.
- **Three sabotages came back green as defence-in-depth pairs** —
  the stored slot is protected by `add` *and* by the `ON CONFLICT` clause
  not touching the column; the ten-stock cap by `add` *and* by
  `free_slot` running out; a validation refusal by its own branch *and*
  by the generic handler. In each case a test was added for the property
  only the first half holds (the returned object's slot; the refusal
  naming the rejected stock; a mistyped field not being reported as "the
  tracked list could not be written", which sends the owner to look at
  their database). §14's lesson: the way to show a pair is load-bearing
  is to break both.
- **One sabotage was itself a no-op** (`… if False else None`) and one
  was a syntax error — both rewritten. A sabotage that does not apply is
  recorded as *not applied*, never as caught.
- The bar fixture was **sabotaged to write no bars**, confirming three
  chart tests go red — otherwise they would have passed without ever
  reading the cache.
- **The upgrade was run, not assumed.** A database was built from the
  schema at `b769a18` (before this session), seeded with the owner's own
  baseline shape, and today's `init_db` run over it — which is what the
  service does on start after `upgrade.sh` pulls:

  | check | result |
  |---|---|
  | tables added by this change | `benchmark_comparisons` only |
  | tables lost | none |
  | existing baselines preserved | 1 of 1 |
  | `PRAGMA foreign_keys` | 1 |
  | the page before any edit | SPY, $2,000.00 from 2026-08-14, worth $2,119.30, +6.0% |
  | after adding AAPL | three lines drawn (BOT, SPY, AAPL); account baseline still `owner_set` 2026-08-14 $2,000 |

  Ten stocks were then rendered together with one deliberately having no
  bars: nine value rows, one dash carrying its raw reason, and
  `labels_outside_viewbox` empty.
- Full suite green offline: **3959 tests**.

### What is NOT claimed

- **No tracked stock has ever been fetched in production.** The chain is
  verified offline: a comparison added now has no line until the bot's
  next daily refresh, and the row says so with the exact upstream
  response.
- **Whether the owner can read ten lines at once is not established by
  the ΔE table** — the table says colour alone cannot do it, which is why
  the dash and the end label exist. Whether eleven lines is *useful* is a
  judgement the owner will make by looking at it.
- Nothing here can size, spend or trade: `grep` over `risk/`,
  `execution/` and `cost/` for the table name returns nothing, and a test
  holds that.

---

## 19. Can a stock added today be compared against the last ten days?

Owner-asked 2026-09-12: *"Does it need to have been actively tracking a
stock or can I get it enter the stock code and it predict or tell me what
it would have done to measure against my current. Eg on day one I'm
measuring my stock on day 10 I want to compare against VUAG for example,
will it be able to recreate the last 10 days correctly?"*

### Yes, and it is backfilled from real closes rather than predicted

Verified by running it: a stock added today with a start date ten days ago
indexes **from the date typed**, not from the day it was added.

```
added VOO, backdated to 2026-09-02
  -> 17 closes, first drawn 2026-09-02, +1.94%
  -> SPY over its own window: +3.56%
```

Two facts make that work, and both were already true:

| what | value | why it matters |
|---|---|---|
| `SIP_START` | 2016-01-04 | a symbol with no cached file bootstraps from here, not from today, so **years** of history arrive on the first fetch and any recent window is inside it |
| `ADJUSTMENT` | `"all"` | dividends and splits are both adjusted, so the series is **total return** — the right basis to compare against an accumulating fund |

Nothing is modelled or predicted. It is the stock's own closes, bought
with the money typed on the date typed.

### THE DEFECT THE QUESTION FOUND: a stock added today waited until tomorrow

`_refresh_tracked_comparisons` guarded on the **date alone**:

```python
if state.get("comparison_day") == today:
    return
...
if not symbols:
    state["comparison_day"] = today    # a quiet day marks itself
    return
```

So on any day the owner had no extra stocks tracked, the first cycle set
the marker and returned — and a stock added that afternoon **was not
fetched until the next day.** The owner would have watched an empty row
for up to 24 hours, which is indistinguishable from a mistyped ticker.

**The marker now says what was DONE, not just when:** the date *and* the
sorted tracked set. Adding or removing a stock stops it matching, so the
very next cycle fetches it — minutes, not a day. It is read **after** the
list, because the list is part of it.

**Why this and not a wake signal from the web form.** The baseline change
solved its equivalent with `force=True` pushed from the caller. This
needs no cross-thread plumbing at all: the tracked set is already in the
database the loop reads every pass, so the marker can simply be honest
about what it covers. Less machinery, and it cannot get out of step.

### VUAG specifically will not work, and the page will say so

VUAG is the LSE-listed, GBP-denominated Vanguard S&P 500 UCITS
accumulating ETF. Alpaca is a US broker serving US-listed symbols, so
there are no bars to fetch. The ticker passes validation — it is checked
by **shape**, deliberately, because a list of known symbols would reject
the first new listing nobody thought of — and then the row shows a dash
with the raw upstream response. **The US equivalent is `VOO`** (same
index, same manager, distributing rather than accumulating, USD). Since
the series is total return, `VOO` and an accumulating share class track
the same thing for this comparison.

Also worth stating: a GBP instrument compared against a USD account with
no FX conversion would be wrong even if the bars existed, so "it does not
work" is the correct outcome rather than a gap to close.

### Verification

- **3 sabotage breakages, all 3 caught red** — the date-only marker
  restored, the quiet pass not marking itself, and the short-circuit
  removed entirely — each verified to still import first.
- The third came back **green on the first attempt**: the test made two
  passes, and without the short-circuit the add still gets fetched, so
  "it got fetched" does not prove the marker is read. A **third** pass
  with nothing changed does, and it was added.
- The behaviour is asserted by **calling the function twice with a real
  state dict**, not by grepping the source — and the state dict is seeded
  non-empty, because an empty dict is falsy and a regression written as
  `state or {}` would pass against one (§14's own lesson).
- Full suite green offline: **3960 tests**.

---

## 20. Saying "US-listed only" in the three places it has to be said

Owner-asked 2026-09-12: *"Can we make it clear only add US stocks that
are listed if not already"* — following §19, where VUAG turned out to
have no bars because it is the LSE listing.

### Why the form cannot simply enforce it

The ticker is validated by **shape**, not against a list of known
symbols, and that is deliberate: a list would reject the first new
listing nobody thought of (house rule 7). So a London ticker **is
accepted**, and only reports itself once the fetch has failed. That makes
this a wording problem rather than a validation one — and the wording has
to admit what the code actually does, or it becomes a promise the code
does not keep.

Said in three places, each for a different moment:

| where | what it says |
|---|---|
| **the input label** | "US-listed ticker, exactly" — visible text, not a `title` attribute a mouse has to find |
| **the note** | the rule, why (the closes come from Alpaca, a US broker), the trap by name (VUAG and VUSA will not work, VOO is the US listing of the same index, and because these series include dividends it tracks what an accumulating share class does), and **that an unknown symbol is still accepted and reports itself** |
| **the empty row** | when a line does not appear: *"either the spelling is wrong, or it is not US-listed"* — because a correctly spelled, real symbol can still have no prices, and blaming only the spelling sends the reader after the wrong thing |

### Five sabotages, ALL FIVE GREEN on the first attempt

Worth recording as a clean instance of §6's opening pattern. The tests
asserted `"US-listed" in block`, `"VUAG" in block`, `"VOO" in block` —
against the whole panel. Those strings appear in **three** places, so
deleting any one of them left the other two and every sabotage passed.

**A test that cannot tell which of three copies it found is not testing
any of them.** Rewritten to extract each region first — the label text
between `<label>` and its `<input>`, the note paragraph, and the specific
`<tr>` for a ticker with no bars — and then all five went red.

The same trap in miniature: the first label assertion searched a window
around the input and would have passed on the `title` attribute alone,
which is exactly the hidden-hover text the change exists to avoid.

### And the note double-escaped its own em dash

The wording went through `prov()`, which **escapes** — so `<b>US-listed</b>
&mdash;` reached the page as visible `&amp;mdash;` and literal `<b>` tags.
Caught by `test_no_double_escaped_entities` and `test_dashboard_ui_pass`,
two tests that exist for exactly this, and `render.py`'s own docstring
already names it: *"Same trap as passing `&mdash;` through `esc()`, which
this dashboard has been bitten by before."* `prov_html` is the helper for
text that is already HTML; the note uses it, and the only interpolation is
an integer constant so nothing unescaped can leak.

Fourth instance of this trap in the file's history, and the first where a
test caught it before the owner did.

### Verification

- **5 sabotage breakages, all 5 caught red** after the tests were pinned
  to regions; all 5 green before. Each verified to still import first.
- Full suite green offline: **3963 tests**.

---

## 21. The prompt cache was never asked for, and the fix is narrower than it looks

Owner-reported 2026-09-13: an Anthropic usage tip offered a **57% saving**
with the warning *"Your prompt cache hit rate is low"*, and asked whether
that is unavoidable given what the bot does.

**It was not unavoidable, and it was not subtle: `cache_control` appeared
NOWHERE in the request path.** The bot already *measured* cache tokens for
costing — `cache_read_input_tokens`, `cache_creation_input_tokens`, even
`ephemeral_1h_input_tokens` — and never set the flag that creates an
entry. The hit rate was zero by construction.

### Measured before building: the cross-call saving is IMPOSSIBLE, and must stay that way

Two candidates' rendered prompts were diffed:

```
prompt A: 5,705 chars   prompt B: 5,705 chars
COMMON PREFIX: 300 chars (~75 tokens) = 5.3% of the prompt
  diverges at: "...CANDIDATE\nTicker: " >>> "AAPL" / "NVDA"
```

The ticker is the **third line**. The cacheable minimum is **1024 tokens
on Sonnet 5** (512 on Opus 5, 4096 on Opus 4.6/Haiku 4.5 — *not*
monotonic across generations), so a cross-call breakpoint would silently
never cache.

**The obvious fix is to reorder the prompt** — standing brief first,
candidate last — and it is **deliberately refused**. That changes what the
model reads first, which changes the judgement this entire project exists
to measure, and would make the live record incomparable with its own
history. A cost saving does not buy that. `test_the_prompt_cache_is_asked_for.py`
asserts the shared prefix is *still* under the minimum, so if someone
reorders the prompt later the suite tells them they have made a
judgement-affecting change rather than a cost one.

### Where the money actually is: WITHIN one call

One research call is **up to four requests** — two exploration turns, a
forced extraction, and a repair — and `messages` **accumulates**, so every
turn resends the whole conversation including web-search results measured
at **34k input tokens median, 166k max**. The prompt is byte-identical
across all of them.

| | cost |
|---|---|
| cache write, 5-min TTL | 1.25× input |
| cache write, 1-hour TTL | 2.0× input |
| cache read | **0.10× input** |

A write costs **+0.25×** once; each read saves **0.90×**. Break-even is
two requests (1.25 + 0.10 = 1.35 vs 2.0); the normal call is three or
four.

**One explicit breakpoint, on the prompt, and nothing else.** Three
reasons, each from the caching reference rather than preference:

- **The growing tail needs no marker from us.** Once a request uses
  caching at all, the API inserts its own 5-minute write after
  server-tool results — so the search output, the expensive part, is
  covered without this code guessing where a second breakpoint belongs.
- **Marking the tail ourselves is the documented way to waste money**: a
  write premium on bytes nothing reads back. Not done until the ledger
  says otherwise.
- **Four breakpoints is the API limit and a call runs up to four turns.**
  One marker that moves nowhere cannot drift into a fifth.

**The 5-minute TTL, not 1-hour.** A read refreshes the timer free, and a
call's turns are seconds apart. The 1-hour TTL doubles the write to 2× and
would only pay *across* cycles — and the cycle is 15 minutes, so each
cycle's first call would write at 2× to serve reads that may never come.
Revisit with a number if the ledger shows enough calls landing inside an
hour.

### The owner's question: does this affect the bot's memory of prior decisions?

**No, and the two are unrelated in a way worth writing down.** Prompt
caching is a **byte-exact prefix match** on the rendered request. It
stores nothing the bot can read back; it is a billing and latency
optimisation, not a memory.

The bot's memory of prior decisions is entirely database-backed —
`research/record.py` renders closed trades and scored refusals into the
prompt, position reviews live in `position_review_checkins`, decisions in
`risk_decisions`. Caching neither adds to nor removes from any of it.

**And caching can never serve stale content.** Because the key is the
exact bytes, the moment the record changes — a trade closes, a refusal is
scored — the prefix differs, the cache misses, and fresh tokens are sent.
There is no mechanism by which a cached entry could feed the model an
out-of-date record. That is the reassurance, and it is structural rather
than something this change had to be careful about.

### Over a long period

Nothing accumulates: entries are ephemeral, there is no storage cost and
no cleanup. As the record grows the prompt grows, so caching becomes
*relatively* more valuable — and each record change is one miss on that
cycle, then warm again. Self-limiting and correct.

**And it helps the thing the owner cares most about.** Searches are the
dominant cost and they are not reduced by any of this; the same searches
simply cost less to carry across a call's turns. The governor's
pre-call estimates are measured from the ledger (§5), so a lower real
cost per call self-corrects downward and **the same $100 cap affords more
research**.

### The change found a regression in the guard it passed through

Wrapping the prompt in a content block **smuggled an empty prompt past
`invalid_payload_reason`**. The string branch had always refused empty
content; the list branch only checked that each block had a `type`. So a
research call with no prompt would have been authorised, paid for, and
refused by the API. Found by this change's own test, not by reasoning
about it. The guard now refuses an empty `text` block.

### Verification

- **8 sabotage breakages, all 8 caught red** — the marker removed, the
  1-hour TTL substituted, the prompt text altered by the wrapper, the role
  changed, a second breakpoint added, the empty-block guard reverted, and
  the "why not across calls" reasoning deleted from the docstring — each
  verified to still import first.
- One existing test reached into `messages[0]["content"]` as a **string**.
  Its intent (the graph context reaches the prompt) still held, so it
  asserts through a shared `_prompt_text()` helper now: **a test pinned to
  the container shape breaks on a change that alters nothing the model
  sees**, which is exactly what happened.
- Full suite green offline: **3979 tests**.

### RECURRING FAILURE, third instance in two days — and this one was new

§18 records killing a sabotage harness mid-run twice. This time the
harness **completed** and still destroyed the work: its `finally` ran
`git checkout -- <file>`, which restores to **HEAD**, not to the edited
state — so it reverted the uncommitted change it was supposed to be
protecting. The tell was `restored: RED` under `8 of 8 caught red`.

**The rule that actually works: commit before sabotaging.** A git-based
safety net only restores what git knows about, so uncommitted work must be
committed (or stashed) first — in-memory backups plus a `git checkout`
fallback is the worst of both, because the fallback silently wins.

### What is NOT claimed

- **No production call has been billed with caching on.** The chain is
  verified offline: the marker is present on every turn, the payload
  passes its guard, the prompt bytes are identical across turns, and the
  ledger records the cache fields. Whether the hit rate actually rises is
  a number the Cost panel will report, and **the 57% in the tip is a
  generic estimate** — the realistic win here is on re-sent context, which
  is most of the input cost but not all of it.
- The first measurement to look at is `cache_read_input_tokens` on the
  Cost page. If it stays at zero across a day with research calls, a
  silent invalidator is at work and this change bought nothing.

---

## 22. The bot did not know what day it was

Owner-asked 2026-09-13: *"is the bot 100% aware of the active current
date and time it is making these searches?"*

**No. None of the three prompts it pays for said what today was.**
Measured from the owner's own bundle (`catalyst-logic-7d-20260913-125402`),
the verbatim `prompt_rendered` for `conj-b3caf562223b246f8844` — CHYM,
called 2026-09-13T00:06 — carried `2026-09-08`, `2026-09-10` and
`Newest signal: 2026-09-10`, and **nowhere the current date**. `grep -n
"today\|now(\|date.today" catalyst/research/prompts.py` returned nothing.

| prompt | what it asked | what it was never told |
|---|---|---|
| research | question 6: *"has the market already consumed these filings?"* | today's date — the question is **entirely** about elapsed time |
| hunt | *"the event must resolve today or later"*, enforced by code against `as_of.date()` | the date `as_of` actually is |
| position review | `next_check_in_days`, *"number of days from today"*; `CLOSES: <date> (fixed)` | today, so also how many days were left |

The hunt case is the sharpest: `as_of` has been a parameter of
`render_hunt_prompt` since it was written and went only to `_digest` and
`_validate`. So the **gate** was measured from a date the **prompt**
never named. §3 row 12 fixed the 88%-rejection defect by stating the
RULE; it did not supply the date the rule is measured against. Both
halves were needed and only one had shipped.

**Why this is quiet rather than dramatic.** The model reasons fluently
about "Sept 10" without knowing whether Sept 10 was yesterday or last
month, and nothing in the reply reveals which it assumed. Read the CHYM
thesis: it argues confidently about a Sept 10 pullback and a Sept 2
acquisition rally and never once states how long ago either was. It is
not possible to tell from the output whether the dating was right.

### What changed

- **Every prompt opens with `RIGHT NOW`**: the ISO date, the weekday and
  the UTC time. The weekday matters — "this is a Saturday" carries that
  EDGAR has filed nothing and the market has been shut for a day.
- **The research prompt states the evidence's age in days**, computed from
  `now - candidate.catalyst_date`. That is the arithmetic `priced_in`
  turns on, done rather than left to the model. A future-dated catalyst
  (every hunted one, by rule) reads `N day(s) in the FUTURE`, never
  `-N day(s) ago`.
- **The market state is stated**, derived from `MarketSnapshot.priced_off`
  rather than from a new parameter — one source of truth, and house
  rule 7: anything that is not `live_nbbo` is not live, including a
  provenance invented later. **`None` is not "closed"**: a missing
  snapshot means nobody looked, and saying the market is shut when nobody
  looked is the same class of error in the other direction.
- **The review prompt says how long it has been held and how many days
  remain.** Six days left and one day left want different answers, and
  it was printing the exit date with no distance to it. An unreadable
  date produces **no day count at all** rather than a confident wrong
  one.
- **`now` is passed in, never read from the clock inside a renderer**, so
  a prompt is reproducible and a test can pin it (house rule 6). It
  falls back to the real clock because *no* date is the defect being
  fixed — a caller that forgets must not silently reintroduce it.

### THE SECOND DEFECT, and this one was a falsehood rather than a gap

`build_closed_market_snapshot` sets `half_spread_bp = 100000` on purpose:
a closed book has no spread, and **zero** is the one value that would
sail through the owner's 20bp hard bound as the tightest book ever
measured (§13, risk review F5). Correct for the risk engine, which
refuses the snapshot on `priced_off` anyway.

**It was going straight into the prompt.** Verbatim, on every
closed-market research call:

```
  - half-spread now: 100000 bp. This is what it costs to get in and out;
    a thesis worth less than the round trip is not a trade.
```

A 1000% round trip kills every thesis that exists, stated under a
heading that claims the number was measured. All four closed-market
calls on record returned `no_trade`. **That is not proof of causation** —
their theses argue coincidence, not cost — but the prompt was asserting
a falsehood about the single number most likely to end the conversation.

Fixed by reporting it as **unmeasurable**, not by hiding it: silence
would be filled by the model, and the round trip is a real cost on the
microcaps this screen surfaces (BWFG measured 99.2bp half-spread and was
correctly refused). The sentinel itself is untouched, and a test asserts
the risk engine still sees `100000`.

### The generalising lesson

**A value chosen to be refused by one consumer was displayed as a
measurement by another.** The docstring explains at length why the number
must be absurd, and the renderer two modules away read it as data. This
is the same shape as §17 (a diagnosis discarded at the point of writing)
and §14 (`orders.decision_id` holding a candidate id): *a field's meaning
lived in one module's comments and every other reader took it at face
value.* When a sentinel is introduced, grep for who renders the field.

### Verification

- Full suite green offline: **4014 tests**.
- **29 sabotage breakages, all 29 caught red**, each verified to still
  import first — 25 in round one (22 red) and the 3 that came back GREEN
  re-run after fixes, plus a fourth for the same wiring.

**The three that were GREEN, and two of them were real test weaknesses
of exactly the kind §6 opens with.** Worth recording because the cause is
general and it will recur wherever a parameter has a sensible default:

| sabotage | why it passed | the fix |
|---|---|---|
| `investigate` stops passing the clock | `render_research_prompt(now=None)` falls back to the real clock, so "the date is present" stayed true with the wiring cut | a behavioural test that passes a date the wall clock cannot produce (`2019-03-14`) and asserts it in the prompt actually sent |
| `run_cycle` stops passing the clock | the call-site grep `"now=now)" in src` found the **other** one, `ensure_history(..., now=now)` | a whole cycle is run with the harness clock and the recorded `prompt_rendered` is read back |
| the hunt clock moved after the hard date rule | **a flawed sabotage** — it deleted the `RIGHT NOW` heading rather than moving the block, so the ordering property was never exercised | rewritten as two edits that genuinely relocate the text to the end of the parts list; it then goes red |

**THE DEFAULT HIDES THE WIRING.** `now=None` reading the real clock is the
right behaviour — a caller that forgets must not silently reintroduce the
defect — and it is exactly what makes "the date is in the prompt" a test
of the renderer rather than of the plumbing. §6's rule (*assert the call
site, not just the function*) needs a corollary: **a substring that also
occurs elsewhere in the same function is not a call-site assertion.**

**And a new process failure: a stale `.pyc` survived the restore.** The
harness rewrote the original bytes, but the `__pycache__` entry had been
written in the **same second** with the same source size, so Python's
mtime+size validity check accepted the sabotaged bytecode and the suite
failed against a source file `git status` reported clean. Ten minutes lost
hunting a phantom. **A sabotage harness must clear `__pycache__` after
restoring**, not merely restore the source.

### What is NOT claimed

- **Whether knowing the date changes any judgement is unmeasured.** The
  claim is narrower and checkable: the model was answering a question
  about elapsed time without being given the time elapsed, and now it is.
  Whether `priced_in` stops being set on 95% of candidates is the number
  to watch, per arm, on the Pipeline page.
- **No production call has been billed with the clock in the prompt.**

---

## 23. The card cut off at the risk engine, and nothing said what was queued

Owner-reported 2026-09-13, on the CHYM decision card and the AAPL
tracked-stock row:

> *"I can see the graph its made but stops at what the deterministic
> engine did and what happened at the broker it just seems to cut off"*
>
> *"the graph is looking good however, these references dont actually
> mean anything to me"*
>
> *"I can see it was doing active research and finding potential stocks
> on 12/09, this is good. What did it do, are any queued up its not clear
> anywhere what it is doing."*
>
> *"why cant it just make the API call to immediately get the historical
> predicted data for tracking on the graph, why do i need to wait a day
> when the data is available"*

### 1. It cut off because there were three arms and the third was the risk engine

Literally. `_spider_groups` returned `saw / concluded / did`, and
`decision_spider` hard-capped `groups[:3]`. **The story of a decision
does not end at the risk engine** — it ends at a fill, or at a sentence
saying why there was never going to be one.

**And the declined case is not an edge case, it is 293 of 294 decisions.**
So "nothing was sent, the risk engine declined" plus "here is what the
stock did without us, or that it is not scored yet" *is* the outcome in
almost every card. Drawing nothing there is what made the page look
truncated.

**A fourth arm needed a fourth colour, and the note in the code said
three was the cap.** So it was measured rather than argued — CIE76 ΔE in
Lab, Vienot LMS simulation for deuteran/protan/tritan, against this
dashboard's own two surfaces:

| | worst normal ΔE | worst CVD ΔE | min contrast |
|---|---|---|---|
| light, 3 arms | 94.2 | 22.5 | 2.82:1 |
| **light, 4 arms** | 42.8 | **22.5** | 2.82:1 |
| dark, 3 arms | 87.2 | 7.8 | 4.66:1 |
| **dark, 4 arms** | 25.5 | **7.8** | 4.66:1 |

**The worst CVD pair is unchanged in both themes** — it is series-1
against series-3, which was already the binding pair and which the fourth
colour does not come between. Normal-vision worst pair falls and stays
well clear of 14.9, the last figure §18 measured as reliable. The fourth
is `--cmp-2`, an **existing** token, so there is no second palette to
drift out of step. Slate was not considered: §18 already rejected it for
reading as chrome.

**The cap is now `[:len(SPIDER_SLOTS)]`, not `[:3]`.** The colour index is
`gi % len(SPIDER_SLOTS)`, so a typed cap and the palette could disagree —
and a fifth arm would have silently redrawn in the first arm's hue.

### 2. The references were machine references, and the describer already existed

| where | was | is |
|---|---|---|
| spider leaf | `Market news (Alpaca)`, accession in the hover | `Chime Financial Stock Pulls Back Thursday`, reference in the hover |
| spider leaf | `edgar fts` | `"credit agreement" "amendment"` |
| unnamed graph entity | `3f9c1ab24e7d4c8fa1b25e6d9c704f11` | dropped from the picture, still in the verbatim table |
| full-record fold | `source event edgar_fts:0001193125-26-385383:credit_amendment fetched …` | the headline, the feed, the time — and a link to the source the feed actually fetched |

**`queries._describe_source` has read those payloads since §10b** — for
the **trade card alone**, which exists for one candidate in seven
thousand. §6's "a helper nobody calls", in its other form: a helper only
*one* caller reaches, on the page almost nobody opens. It is `describe_source`
now, takes the payload as stored or as JSON text, and the spider and the
full record both call it.

**`edgar_fts` had no entry in `SOURCE_LABELS`,** so it read as
`edgar fts` — and it is the feed behind the conjunction arm, which took
48% of the research budget. The one machine name most likely to be on the
page was the one missing from the table. Found by rendering the owner's
own card, not by a test.

**Readability is a RULE, not a list of id formats** (house rule 7): a
label whose letters do not carry it, or which contains no word of three
letters, is a machine reference whatever scheme produced it. A hand-written
list of formats mislabels the first format nobody thought of.

**And the rule's first version was wrong, found by rendering.** It
required `word.isalpha()`, and an EDGAR full-text match is stored as
`"credit agreement" "amendment"` — **every word carries a quote**, so
`isalpha()` was False for all of them and a perfectly readable phrase was
thrown away in favour of the feed's machine name. It counts letters
*inside* each word now.

### 3. "What is it doing" — the figures were all on disk and nothing assembled them

The Pipeline page counts a **lifetime** population: 6,999 candidates,
299 researched. That answers *what has happened* and structurally cannot
answer *what is happening*. And its largest single drop reason is
**`deferred_max_research_per_cycle` at 6,581** — that IS the queue,
named, counted, and never once described as one. The owner read a
six-thousand-line loss with nothing anywhere saying those candidates were
still in the running.

`working_on()` now sits **above** the funnel on `/funnel` and answers four
questions in the order a reader asks them:

1. **When did it last spend anything, and when was the newest candidate
   built** — two separate facts, because discovering-but-not-researching
   is a different problem from doing neither, and one number cannot say
   which.
2. **Queued / judged-and-waiting-for-the-open / finished / paid calls
   today**, each counted from rows. The weekend state gets its own count:
   it is not queued (it has been judged and costs nothing more) and not
   finished (no risk decision).
3. **The rate, stated as arithmetic the reader can check** — slots per
   cycle × cycle length — and explicitly **not a countdown**, because the
   screens rebuild the candidate list every cycle so the queue grows while
   it drains. Plus that nothing in the queue is discarded.
4. **The next eight names.**

**TRIED AND CORRECTED BEFORE SHIPPING:** the first version ordered the
next-in-line list by `discovered_at DESC` and captioned it *"the order
the belt takes them in"*. **That was an unverified claim about code, and
`interleave_by_arm`'s own docstring contradicts it** — the belt
round-robins one per arm per round, and within an arm it preserves the
live builder's order, which the database cannot replay. So the rotation
is now applied **by calling the cycle's own function**, and the caption
admits what cannot be reproduced instead of papering over it. Caught by
reading the function before describing it (house rule 1).

### 4. The new stock is fetched on the add, and the wait was already not a day

§19 had cut it from a day to **one cycle** by keying the refresh marker on
the tracked SET as well as the date. But one cycle is up to fifteen
minutes of an empty row, and **an empty row is indistinguishable from a
mistyped ticker** — the state this dashboard has been reported for twice.

So `track_stock` now fetches immediately, through the **same**
`refresh_comparisons` the scheduler calls. Three things make that safe
rather than clever:

- `refresh_benchmark` promises not to raise and `refresh_comparisons`
  wraps it again, so a failed fetch cannot cost the row.
- **The row is committed before the fetch runs.** A failure, a timeout or
  a dead process leaves the stock tracked and the scheduler picks it up
  next cycle exactly as before — this is an accelerator, never the only
  path. A test asserts that with a fetch that raises.
- It writes a bar cache file and nothing else. `grep` over `risk/`,
  `execution/` and `cost/` for the function returns nothing, and a test
  holds that.

The sentence the owner gets says **what happened**, not what was
attempted: bars written and the feed, or the refusal with the raw upstream
response and *"either the spelling is wrong, or it is not US-listed"* —
which is where a London ticker such as VUAG reports itself, since the form
accepts it by shape on purpose (§20).

### The generalising lesson, and it is the fourth time

**A fact the system already had, one caller away from the page that needed
it.** §17 (the diagnosis discarded at the point of writing), §14
(`orders.decision_id`), §22 (the spread sentinel), and now the source
describer reachable only from the trade card. In every case the work was
done and the wiring was missing. **When a panel is reported as confusing,
grep for whether the fact already exists somewhere else in the codebase
before writing anything new.**

### Verification

- Full suite green offline: **4087 tests**.
- **36 sabotage breakages, all 36 caught red**, each verified to still
  import first. 31 red on the first pass; **four of the five greens were
  real test weaknesses** and one was a flawed sabotage.

| green | why it passed | the fix |
|---|---|---|
| the uuid guard (mindmap) | asserted against `trace_simple`, which does not render the mindmap at all | re-asserted on the FULL record, reading the SVG's `<text>` elements |
| the uuid guard (spider) | the spider reads `subject_label` only, so `subject_entity_id` **cannot reach it** | the same defect wearing the other hat is reachable — an entity whose stored `display_name` IS a machine reference. Seeded, and the guard is now load-bearing |
| the weekend-view count | no LIVE view in the fixture, so `priced_off != 'live_nbbo'` → `1=1` changed nothing | a live view with no decision — an ordinary Tuesday — seeded; the counts moved 54/6 → 53/7 because of it |
| dash-not-zero | used a MISSING database, which returns early and never reaches the per-count handler | a database that opens with one table present does reach it, and the error must name the table that was not there |
| "nothing is discarded" | **a flawed sabotage**: it blanked the last string fragment while the sentence lives in an earlier one | retargeted at the sentence, then red |

### AND PINNING THAT TEST FOUND A REAL DEFECT IN MY OWN RULE

Worth its own heading because it is the second time in one change that a
readability rule was wrong, and the first version of the fixture could
not have caught it.

The fixture used entity ids `g1`..`g4`. **Production writes
`uuid.uuid4().hex`.** With realistic ids the mindmap fallback drew a box
reading `a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6` — and measured, that string is
**sixteen letters in thirty-two characters**, because its digits happen to
be mostly `a`–`f`. The rule at that point was *"letters carry at least
half the string, plus a token containing three letters"*, and a hex id
like that clears **both** halves.

The rule is now about **word shape**: a word is a run of letters with
punctuation stripped from its ends, and a label is readable when it has at
least one word **and** words carry at least half the characters. No hex
case and no list of id formats (house rule 7), so:

| label | readable |
|---|---|
| `"credit agreement" "amendment"` | yes |
| `Bern Richard (CEO) bought 141,000 shares at $70.96` | yes |
| `a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6` | no |
| `61720763:CHYM` | **no** — a reference with a real word stuck to it is still a reference |
| `0001193125-26-385383` | no |

**Three defects in this change were found by running it, none by
inspection:** `isalpha()` discarding the quoted EDGAR phrase, the missing
`edgar_fts` label, and the hex id clearing the letter-fraction rule. Plus
the unverified claim about the belt's order, found by reading
`interleave_by_arm` before describing it.

**A fixture that cannot produce the input the owner reported cannot test
the fix.** `g1` is unreadable under every version of the rule, so the
fixture agreed with the bug — the same shape as §14's foreign-keys-off
fixture, in miniature.

### AND THE UPGRADE CHECK FOUND A FOURTH, which no test could have

Run rather than assumed: a database built from the schema at `e7d4961`
(before this session), then today's `init_db` over it — what the service
does on start after `upgrade.sh` pulls.

| check | result |
|---|---|
| tables added by this session | **none** |
| tables lost | none |
| existing rows preserved | every one |
| `PRAGMA foreign_keys` | 1 |
| every page this session touched | rendered, no exception |

**What it found:** a view written before `research_view_context` existed
has no provenance row — so on the new panel it was in **neither** the
weekend count, **nor** the finished count, **nor** the queue. It fell out
of the arithmetic entirely, and every candidate on that database is in
that state. That is the shape of every "the numbers do not add up" report
this dashboard has had.

Fixed by counting `awaiting_decision` — any view with no risk decision —
and keeping the weekend figure as its **named subset** rather than as a
stand-in for it. A test now asserts
`queued + judged + finished == candidates`, so a fourth state added later
cannot quietly fall out of the total. And an unrecorded provenance is
**not** read as "the market was shut": unknown and closed are different
facts, the same asymmetry §22 needed for `market_is_live`.

**38 sabotage breakages, all 38 caught red.** Full suite green offline:
**4089 tests**.

Two of the original 37 came back **NOT APPLIED** on the final pass, and
that is recorded as not applied rather than as caught (§18): they targeted
the *old* readability rule, which the hex-id defect above replaced. They
were retargeted at the word-shape rule — drop the guard entirely, drop the
words-carry-half condition, and count a token with digits in it as a word
— and all three go red. **A sabotage suite rots when the code it points at
changes**, and the tell is a NOT APPLIED count, which is why the harness
asserts every replacement's expected occurrence count.

### What is NOT claimed

- **No tracked stock has ever been fetched from the dashboard in
  production.** The chain is verified offline with an injected transport.
- **Whether four arms is more readable than three is a judgement the
  owner will make by looking at it.** The ΔE table says only that the
  fourth colour costs nothing measurable in separability, and every arm
  carries a visible text label, so colour is never the identifier.

---

## 24. The weekend price came from a 30-day cache, not from Alpaca

Owner-asked 2026-09-13, after the ACVA decline in §23's bundle:

> *"are we ensuring the pricing info it is pulling is accurate aswell, i
> dont want it to read a price that may not be life, hence we should
> allow it to tap into alpaca or somehow get alpaca to give a live read"*

### During market hours this was already true, and is untouched

`build_market_snapshot` asks Alpaca for the live NBBO **every cycle** and
refuses a quote that is absent, undatable, older than `MAX_QUOTE_AGE`
(10 minutes), non-positive or crossed. The mid is then cross-checked
against the cached close — beyond ±35% flagged, beyond 5x refused and no
order placed. And `risk/evaluate.py` accepts that provenance and no
other. Nothing here changed.

### While the market was shut it was NOT true, and the number is 30

`bar_history.MAX_CACHE_AGE_DAYS = 30`. A symbol's history is refetched
only when the file is over a month old — **correct for what that cache
exists for**, because a 95th-percentile daily move and a worst-case gap
measured across three years barely move in a month.

**But §13's weekend feature made the same file the source of THE PRICE
THE MODEL REASONS ABOUT**, and a price may not be a month old.

| path | freshness guard, before this |
|---|---|
| live quote | refused above **10 minutes** |
| weekend close | **none at all** — `rows[-1]`, whatever its date |

**Measured, from the owner's own bundle.** ACVA was rendered into the
prompt at `last close: $7.22` while the stock traded around **$10.43**
after an all-cash tender offer at $10.50. `$10.43 / 1.44 = $7.24`, so the
cached close predated the announcement. The model caught it only because
it happened to search and then disbelieved its own input:

> *"The market data snapshot given here (last close $7.22) conflicts
> sharply with every public source (all citing ~$10.43 post-pop), so
> either the price feed is stale/broken for this name or there is a data
> error; either way I cannot rely on it to size an edge."*

**That is luck, not a guard.** And `price_action._rows` has returned the
row's DATE as element 0 since it was written — the fact needed to catch
it was in the tuple and nothing read it. Fifth instance of §23's closing
lesson.

### What changed

1. **Alpaca is asked first.** `_broker_daily_close` requests a 12-day
   window and takes the **newest bar by date**, not the last element —
   page order is the feed's business, not ours.
2. **The cache is a NAMED fallback.** Two provenances now:
   `broker_daily_close` and `cached_daily_close`. Both end in
   `daily_close` and `evaluate` refuses anything that is not exactly
   `live_nbbo`, **by rule not by list**, so a value added later is refused
   for sizing the moment it exists. What the suffix buys is the page and
   the prompt being able to say which — *"Alpaca said"* and *"a file on
   disk said"* are not the same claim, and the second one was the wrong
   one.
3. **The close carries its date**, and the prompt states it with its age:
   *"last close: $10.43, dated 2026-09-11 - 2 day(s) before today. THIS IS
   NOT A LIVE QUOTE …"*
4. **A close older than the market has plausibly been shut is REFUSED**,
   with the date, the age and the source in the reason (house rule 3).
   `MAX_RESEARCH_CLOSE_AGE_DAYS = 7`, derived: the longest scheduled US
   closure is four calendar days (Friday close to Tuesday open across a
   Monday holiday), so a week clears it with room and still catches the
   30-day case by a wide margin. A test asserts the bound exceeds four.
5. **The model is told to trust its own search over this number** if the
   two disagree. ACVA was saved by exactly that instinct; it should be
   instruction rather than luck.

### THE FIXTURE WAS WRITING A CACHE 553 DAYS STALE

The guard went red on eighteen existing weekend tests the moment it
landed, and the reason is the finding: `test_the_weekend_is_not_wasted`
anchored its bars at **2024-01-01**, so with 400 rows the newest close
landed on 2025-02-03 — **553 days before that module's own `NOW`** — and
the old code read it as the current price without complaint.

**So no test in this suite could ever have detected this bug**, because
every fixture was already in the failure state and the code had no
opinion about it. The fixture now writes up to *yesterday*, which is what
`ensure_history` actually produces (`end = now - 1 day`, deliberately —
a partial session is not a session).

Same shape as §14's foreign-keys-off fixture and §23's `g1` entity ids,
and that is now **three instances**: *a fixture that cannot produce the
state the owner hit will agree with the bug.*

### Also found and fixed while reading: an enumeration where a rule belongs

`panels._why_not_researched` compared `priced_off == "daily_close"`. The
moment a second closed-market provenance existed, a weekend view would
have fallen through to *"waiting for the risk engine"* — the opposite of
true. Now `off and off != "live_nbbo"`, and it names which source
produced the close (house rule 7).

### The adversarial read, and the one thing it changed

Full read in the commit body. What it changed: the new field was going to
be `MarketSnapshot.as_of`, and **`PortfolioState.as_of` already exists** —
a `datetime` rather than a `date`, and the value `kill_switches` measures
staleness against. Two fields sharing that name, of different types, one
load-bearing for a kill switch, is a trap for the next reader. Renamed
`close_date`.

What it cleared: `close_date` reaches the prompt renderer and nothing
else (`grep` over `risk/`, `execution/`, `cost/` finds only its own
definition); the closed-market path only ever **reads** the bar cache, so
it cannot become a second writer and corrupt the history sizing measures
a stop from (the failure §18 found in `BarCache`'s shared metadata); and
the extra request is a market-data GET, so it spends no part of the API
budget the governor bounds.

**The one thing that got worse, stated plainly:** there is now a broker
call per candidate per closed-market cycle. If Alpaca's data API is down
at the weekend every candidate falls back to the cache — the old
behaviour plus a failed fetch. The downside is latency and a log line,
not a changed decision.

### Verification

- Full suite green offline: **4123 tests**.
- **26 sabotage breakages, all 26 caught red**, each verified to still
  import first. 23 red on the first pass.
- **18 existing weekend tests went red the moment the guard landed**,
  which is how the 553-day fixture was found.

**The three greens, and two were the same shape as §22's:**

| green | why it passed | the fix |
|---|---|---|
| the cycle stops passing the broker | the test GREPPED `run_cycle` for `refused=stale` — which survives that edit | a real closed-market cycle: a 40-day-stale cache plus a broker holding yesterday's close can only research if the broker was passed |
| the cycle stops recording the stale refusal | same grep; `closed_market_close_too_stale` survives `if False:` too | the same cycle asserts the reason reaches the funnel **with its numbers** |
| a non-dict bar is not skipped | **defence in depth** — `Broker.get_daily_bars` already filters with `isinstance(b, dict)`, so a bare string cannot reach the inner guard through a real broker | recorded as such (§14, §17) and the property only the inner guard holds is tested directly: a caller handing the helper a list of its own would hit `AttributeError`, which is **not** among the exceptions the loop catches and would escape into the cycle |

**A substring still present in the source is not a behaviour.** That is
now the second change in a row where a call-site grep walked past a
sabotage, and the corollary to §6's rule is worth stating once more:
assert the OUTCOME when the outcome is reachable offline, and this one
was — the whole cycle runs against an injected clock, an injected broker
and a cache dated by the test.

### Verified end to end, not asserted

The ACVA case driven through a full cycle on a database built from the
schema at `3322198` and upgraded by today's `init_db` (no tables lost,
every row preserved, `PRAGMA foreign_keys` = 1):

| | |
|---|---|
| the cache held | **$7.22**, 40 days stale |
| Alpaca held | **$10.43**, yesterday |
| the model saw | **$10.43**, via `broker_daily_close` |
| orders placed | **0** — the market is shut, and that has not moved |

And both failure branches, on fresh databases:

| Alpaca | cache | outcome |
|---|---|---|
| down | 40 days old | researched 0, funnel: `closed_market_close_too_stale: newest close is 2026-08-04, 40 day(s) old, over the 7-day bound (cached_daily_close)` |
| down | Friday's close | researched 1, funnel: `researched_while_closed_awaiting_open` — **the feature still works when the broker cannot be reached**, which is the point of keeping the cache as a fallback rather than removing it |

### What is NOT claimed

- **This has never run against the real Alpaca at a weekend.** Every
  broker in the tests is an `httpx.MockTransport`. The first thing to look
  at is whether a weekend prompt reads `broker_daily_close` or
  `cached_daily_close` — if it is always the latter, the fetch is failing
  and the fallback is hiding it.
- **It does not make the weekend price LIVE.** There is no live price when
  the book is shut. It makes it the *newest close that exists*, dated, and
  refuses it when it is not.

---

## 25. The upgrade rolled back on my own test, and the assertion could only fail by crashing

Owner-reported 2026-09-13, with the upgrade's own output:

> *"PUTTING THE OLD VERSION BACK … WHY: The new version failed its own
> tests, so it is not safe to run your money through it."*
>
> `FileNotFoundError: [Errno 2] No such file or directory:
> '/home/user/catalyst'`
>
> `1 failed, 4119 passed, 3 skipped, 1 warning in 74.77s`

**The safety net worked exactly as designed and the failing test was
mine.** `upgrade.sh` runs the full suite after pulling and rolls back on
any failure, so nothing broken reached the money — but the version that
did not ship contained §24's weekend-price fix, which the owner was
waiting on.

### The test, and the three defects in nine lines

```python
def test_nothing_here_can_size_spend_or_trade(self):
    out = subprocess.run(
        ["grep", "-rn", "_fetch_one_comparison_now",
         "catalyst/risk", "catalyst/execution", "catalyst/cost"],
        capture_output=True, text=True, cwd="/home/user/catalyst")
    assert out.stdout.strip() == ""
```

| # | defect | measured |
|---|---|---|
| 1 | **a hard-coded absolute path** — the sandbox it was written in | `subprocess.run(..., cwd="/home/billy/Desktop/catalyst")` → `FileNotFoundError: [Errno 2] No such file or directory` |
| 2 | **shelling out to `grep`** for four lines of Python | grep's presence, its exit codes and its path resolution are three failure modes, none of them the thing under test |
| 3 | **THE VACUOUS PASS, and it is the worst of the three** | run from a directory where those paths do not resolve: `returncode 2`, `stderr "grep: catalyst/risk: No such file or directory"`, **`stdout ''`** — so `assert stdout.strip() == ""` **passed while reading no files at all** |

Defect 3 is the one worth keeping. The assertion's only failure mode was
a crash: whenever the search worked it found nothing, and whenever it
did not work it also found nothing. §6's opening row — *a test that
cannot fail is not a test* — in a form the sabotage rounds could not
catch, because sabotaging the **production** string it guards would
still leave the test green.

**And two sibling guards had the same shape.**
`test_tracking_ten_stocks_at_once.py` greps the same three directories
with **no** `cwd` at all, which silently depends on pytest being invoked
from the repository root; from anywhere else it searched nothing and
passed.

### What changed

`tests/source_guard.py` — pure Python, root derived from `__file__`, and
**it proves its own haystack before asserting anything about it**: the
directory must exist and must contain at least one `.py` file, or the
call raises with the resolved path in the message. An empty result then
means *searched and found nothing*, never *searched nothing*. Five tests
hold that, including the positive half (`MONEY-CRITICAL` **must** be
found under `catalyst/risk`) — without which every guard built on the
helper could be satisfied by a search that reads no files.

### The generalising guard, stated as a rule rather than a list

`test_no_file_names_this_checkout_by_absolute_path` walks `tests/`,
`catalyst/` and `scripts/` and fails on any string literal that **is**
this checkout's own resolved location, or that points **inside** it or
inside the running user's home directory. Both facts are **derived at
runtime** — house rule 7, because no enumeration of `/home`, `/Users`,
`/root` generalises: the offending path is whatever directory the
checkout happens to live in, which is only knowable by asking.

**It found a second offender immediately:** `scripts/fetch_sic.py:22`
carried `REPO = pathlib.Path("/home/user/catalyst")`, four lines under a
correctly derived `ROOT`. Now derived too.

The assertion is **vacuous today** — there is nothing left to find,
which is the same shape as the assertion that caused all this. So the
rule is exercised against synthetic input separately: a literal that is
the checkout, one deeper inside it, one under home, and four that must
NOT be flagged.

### TRIED AND REJECTED: matching the root ANYWHERE in a literal

The first version matched a root anywhere in the string, reasoning that
`"cd /the/checkout && pytest"` is exactly as unportable as the `cwd=`
that broke the upgrade. **Measured, it flagged two things that are not
bugs:**

| flagged | what it actually is |
|---|---|
| `catalyst/dashboard/render.py:88` | a **CSS comment**, inside the one big style literal, quoting a path the sidebar once rendered badly. Prose embedded in a large literal is not something an AST can separate from code |
| `tests/test_scaffold.py` | **this guard's own fixture**, writing `"/root"` while explaining why short roots need different treatment — because on this machine `Path.home()` **is** `/root` |

Section 17's generic-word trap in a new coat: a rule that cries wolf
gets silenced by the next reader rather than obeyed. A docstring
exclusion was tried as the fix for the first row and does not reach it —
the offending text is a comment inside a CSS string, not a docstring.
So the rule is start-anchored, and:

- **a root must be followed by a separator** to count, which also stops
  `/some/where/catalyst-backup` reading as being inside
  `/some/where/catalyst`;
- **the checkout counts on exact equality too**, because
  `cwd="/home/user/catalyst"` is precisely the literal that failed;
- **a bare home directory does not**, because it is not a path into
  anything and is what a fixture naturally writes.

The docstring exclusion was then **removed as machinery no test could
make load-bearing**: under a start-anchored rule no docstring in this
repository is flagged, since `source_guard.py`'s own explanation
mentions the path mid-sentence. Sabotaging it came back GREEN, which is
how it was caught.

### Why no sabotage round caught this, and the rule that follows

Every sabotage this project runs breaks the **code** and checks the test
goes red. This defect was in the **test**, and in the direction where
breaking the code changes nothing. The check that catches it is
different in kind, and it is cheap:

**RUN THE SUITE FROM A DIFFERENT ABSOLUTE PATH BEFORE SAYING IT IS
GREEN.** Measured, on this change:

| where | old guard | new guard |
|---|---|---|
| the development checkout | pass | pass |
| a copy at an unrelated absolute path | **pass** (the hard-coded path still exists on *this* machine) | pass |
| a directory where `catalyst/risk` does not resolve | **pass, having searched nothing** | raises |
| a machine without `/home/user/catalyst` | **FileNotFoundError** | pass |

Note row two: copying the repo elsewhere on the same machine did **not**
reproduce the owner's crash, because the hard-coded directory still
existed. What reproduced it was pointing the same call at a path that
does not exist here. **A path-portability bug does not reproduce by
moving the code; it reproduces by removing the path.**

The first cross-path run also failed for an unrelated and instructive
reason: the copy was made with `git ls-files`, which omitted the
brand-new untracked helper, so eight tests failed on a missing import.
**A "does it work elsewhere" check built from tracked files only cannot
see the file you just added.**

### Verification

- **11 sabotage breakages, all 11 caught red**, each verified to still
  import first. The harness **refuses to run against a dirty working
  tree** — section 21's lesson made mechanical, because a `git checkout`
  restore only restores what git knows about, and it stopped this round
  twice while the rule was still being changed.
- Both defects of the old guard reproduced by running them, not argued:
  the `FileNotFoundError` against a path that does not exist here, and
  the vacuous pass with its `returncode 2` and empty stdout.
- The three touched test files run green from
  `/tmp/.../scratchpad/elsewhere`, an unrelated absolute path, and so
  does the whole suite from there.

### A NEW PROCESS FAILURE: committing while the suite runs invalidates it

The first full run came back with **two failures that were not a
regression at all**:

```
FAILED test_version_moves.py::TestThePatchMovesByItself::
       test_it_counts_commits_since_the_series_changed
FAILED test_version_moves.py::TestTheOwnerCanTellTwoDeploysApart::
       test_a_dirty_tree_says_so
```

Both compare **live `git` state** against `catalyst.__version__` and
`catalyst.__build__`, which are computed **once at import**. I committed
three times while that run was in flight, so the commit count moved and
the tree went from dirty to clean underneath it. Re-run on a settled
tree, both pass.

**The rule: do not commit, edit or stash while a suite run you intend to
trust is in flight.** This project has two tests that are *correctly*
anchored to the repository's own live state — that is their whole
purpose, so they cannot be loosened — and any working-tree change during
a run makes them report on a repository that no longer exists. It is the
mirror image of house rule 6: instead of a fixture drifting out of a
window, the *world* drifts out from under the fixture.

It also cost a wrong conclusion for a few minutes: two red tests in
`test_version_moves.py` look exactly like a real break, and the only way
to tell was to re-run them once nothing was moving.
- Full suite green offline, **4138 tests**, run twice: once in this
  checkout and once from an unrelated absolute path.

### What is NOT claimed

- **The owner's upgrade has not yet been re-run.** What is verified is
  that the failing assertion no longer depends on any path this machine
  happens to have, and that the same class of literal cannot re-enter
  `tests/`, `catalyst/` or `scripts/` without failing the suite.
- **The other 21 test files using raw `sqlite3.connect`** (§14) are
  still a known blind spot. Untouched here on purpose.

---

## 26. A quiet feed read as a broken one all weekend, and the hold-longer question

Two things, 2026-09-13. The owner asked whether a high-conviction
position can hold past its exit date if the bot re-evaluates, and
reported the Pipeline page still showing:

> *"2 Insider trades (SEC Form 4) could not be read, 2 times since
> 2026-09-12T17:02 — the server returned a web page instead of data
> (titled "SEC.gov | File Unavailable")"*

### The hold-longer answer: yes at entry, no after it, and the asymmetry is the point

| when | who decides | bound |
|---|---|---|
| **at entry** | **Claude**, via `view.expected_holding_days` | clamped to 1 day minimum and `HARD_BOUNDS.max_hold_days` = **31** |
| **after entry** | nobody — `bring_exit_forward` refuses any `new_date >= original` | two independent checks, in `apply_review` and again at the point of writing |

So a thesis saying "this resolves in 25 days" already gets 25 days
rather than a fixed default, and the dashboard says whether the date came
from the model or from the measured per-catalyst fallback. What cannot
happen is a live position being extended.

**Recommended against changing that, on this project's own evidence.** A
candidate here scored **0.82 conviction** on a compelling thesis whose
conclusion was *do not trade* (§13). Persuasiveness and correctness are
different properties, and the position that most wants more time is the
one that has not worked yet — a losing position always has a story
attached. It also holds one of five slots, which is what caps the rate
near ten trades a month (§11).

**The right lever is the entry estimate, not a live extension.**
`holding_period_estimate` is adaptive: bounded 1–21 days, minimum 15
closed trades, at most 2 days per adjustment, on closed scored outcomes
only. Evidence moves it; a thesis cannot talk it up.

**And the measurement that would settle it does not exist.** Audited:
nothing tracks what a position did *after* a hard exit
(`grep -rn "after_exit\|post_exit\|would_have"` → nothing). That is the
exact analogue of the refusal tracker, and without it "should it have
held longer?" is unanswerable. One closed trade on record, exited by the
clock. **Open, and cheap: score hard exits against what the stock did
next.**

`max_hold_days = 31` is a hard bound, so raising it is the owner's. Not
proposed — there is no evidence either way.

### The feed fault: the SENTENCE was right, the STATE was wrong

§17's fix shipped the day before and the diagnosis it produced is
correct. What was wrong is that the row was still under NEEDS ATTENTION
more than a day later for a feed that was working.

**Measured against the real SEC before changing anything**, because the
first guess — "this is the weekend index, already handled" — was wrong:

| request | result |
|---|---|
| Friday's daily index | HTTP 200, real data |
| **Saturday's** daily index | HTTP 403 + `AccessDenied` — routine absence, already handled |
| an absent filing | HTTP 404 + `NoSuchKey` — also handled |

The Saturday body is **gzipped XML**, not the HTML page the owner saw. So
`"SEC.gov | File Unavailable"` is a genuine transient sec.gov outage, and
two occurrences across ~108 weekend attempts is a blip that was already
over.

**The defect is that nothing could say it was over:**

```sql
SELECT COUNT(*) FROM raw_events WHERE source = ? AND fetched_at > ?
```

That is how the panel decided a fault was resolved — *did this feed
produce ROWS since it failed?* At a weekend the Form 4 feed correctly
produces **zero** rows, because EDGAR publishes no daily index. And a
successful read that yields nothing wrote **no row anywhere**: only
failures were recorded. So a Saturday-evening outage could not clear
until EDGAR next published on the Monday — up to **48 hours** of reported
damage for a bot doing its job. Third instance of the "routine attrition
must not look like damage" failure CLAUDE.md says has already cost real
debugging time twice.

**"It answered" and "it had something to say" are different facts, and
only one was recorded.** `feed_reads` records the answer itself, empty or
not, as a side table (never a column on a hot one). `item_count` is kept
because **zero is the interesting value** and house rule 3 wants it
stated rather than inferred from an absent row.

Measured, the owner's exact case rendered both ways:

| | NEEDS ATTENTION block |
|---|---|
| two failures, no read recorded | **rendered** (correctly — this is the pre-fix state) |
| the same two, then four empty reads | **gone**, and listed under "failed and recovered" |

**Where the read is recorded matters, and it is not where it looks like
it should go.** `run_cycle` sees the whole fetch as one call — but a Form
4 `RateLimitBlocked` records an error and then `return []`, which
`run_cycle` receives as a perfectly successful empty fetch. Recording the
read there would have cleared the fault written one line above it, every
time. So it is recorded per-feed in the scheduler, where the truth is
known, and a test drives a real blocked cycle to hold that.

### AND A SECOND, PRE-EXISTING BUG: `feed_healed` was computed and thrown away

`funnel()` built the recovered list forty lines above its own `return`
and **never passed it to the `Funnel` object.** The field existed with a
default, the panel renders a section from it, and the panel's own
paragraph promises *"A failure it recovered from is listed separately
below, not here."* That section has been empty since it was written.

Not harmful — the important half (not in NEEDS ATTENTION) worked — but it
is an unkept promise on the page, and it is the **sixth** instance of this
project's most recurring defect: **the work done and the wiring missing**
(§12, §14, §17, §22, §23). Found by a test failing for a reason unrelated
to what it tested, not by inspection.

### And the fault line named only one end of the range

*"2 times since 2026-09-12T17:02"* gives the **first** occurrence, so two
blips that stopped an hour later read as something that started then and
never stopped. It now names both ends, and says "the same time" when they
coincide rather than printing one timestamp twice.

### Verification

- Both directions reproduced by rendering the owner's own case, not
  argued.
- The scheduler wiring asserted by running a **real cycle** with
  `run_cycle` stubbed to call the injected feed — §22 and §24 both had a
  call-site grep walk past a sabotage, so the outcome is asserted, never
  a substring.
- One existing test correctly pinned the old wording
  (`"2 times since" in ...`) and was updated to assert the **property** —
  both timestamps present — rather than the phrasing.
- **8 sabotage breakages, all 8 caught red**, each verified to still
  parse first: the read table not consulted; the `raw_events` fallback
  dropped; any read ever clearing a fault; the source filter removed;
  `feed_healed` thrown away again; the fault line naming one end;
  the Form 4 success path not recording; and the read recorded **before**
  the fetch, so a block would clear its own fault.
- **The upgrade run, not assumed.** Today's `init_db` over the schema at
  `aa57343`: `feed_reads` added, no table lost, every row preserved,
  `PRAGMA foreign_keys` = 1. On the upgraded database the owner's fault
  **still shows until the next cycle records a read, then clears** —
  checked by running it, because "it self-heals in fifteen minutes" is a
  claim about behaviour.
- **Full suite green offline: 4145 tests.** The run reported exactly two
  failures, both in `test_version_moves.py`, and both were the
  mid-run-commit artefact §25 records — re-run on a settled tree with
  zero uncommitted files, 37 passed. That is the second time in one day
  that lesson has been needed, which is why it is in §6.

### What is NOT claimed

- **No weekend has run with `feed_reads` in place.** The chain is
  verified offline and against an upgraded copy of the schema. The first
  thing to look at is whether the Pipeline page goes quiet within one
  cycle of the upgrade; if the fault persists past that, the scheduler is
  not reaching `_record_feed_read` and the fallback is hiding it.
- **Whether the bot should hold a high-conviction position longer is
  unmeasured**, and cannot be measured until hard exits are scored
  against what the stock did next. That is the open item, not the answer.
