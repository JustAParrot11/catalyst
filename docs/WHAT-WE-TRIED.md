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
