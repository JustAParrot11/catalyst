# Catalyst trading bot

An autonomous bot that trades US equities unattended. **Claude finds the
opportunities, researches them and decides whether they are worth taking.
Deterministic code decides how much money is at stake.**

Full spec: @docs/BUILD-BRIEF.md — the original brief, kept as written.
Where this file and the brief disagree, **this file is what the code
does**; the brief is the goal it was built toward.
Facts that cost real money to learn: @docs/TRAPS.md — read before
writing any cost tracking, data feed, or broker code.
**What has already been tried: @docs/WHAT-WE-TRIED.md** — the project's
memory. Every blocker found and fixed with its evidence, the strategy
bake-off results, the per-arm production record, the measurements that
were wrong, and the failure patterns that keep recurring in this work.
**Read it before diagnosing why the bot is not trading**, before
re-tuning a threshold, and before adding an arm or a gate — the reason
for zero trades has moved fourteen times and eleven of those were
plumbing, not strategy.

---

## What the bot actually does, as of 2026-08-17

Every 15 minutes, unattended:

1. **Feeds collect evidence.** EDGAR Form 4, EDGAR full-text search,
   Federal Register, ClinicalTrials.gov, Alpaca news. Free, keyless
   where possible, cached. This costs nothing per item.
2. **Two things turn evidence into candidates.**
   - The **mechanical screens** — Form 4 insider clusters, cross-feed
     conjunctions, and (since 2026-08-30) post-earnings drift from the
     first-filed XBRL of every 10-Q/10-K filer. The first and last are
     line-for-line the arms that were backtested, so their measured
     edge means something; drift graded better (57% out of sample,
     8.8% max drawdown vs 49% and 41%).
   - **Claude's hunt** — twice a day at the current cap, Claude reads
     the raw feed and, since 2026-09-05, **goes looking**: it runs its
     own EDGAR full-text searches for phrases it chooses, opens filings
     to read the body where the dates live, and checks the news on a
     name. Up to eight tool calls a hunt, every turn through the
     governor. It may only cite events that exist — but what a tool
     finds is written to `raw_events` and counts, so the rule is
     unchanged and the reach is not.

     Since 2026-09-11 it also has **web_search, five times a hunt**, and
     a brief for using it: **second-order chains**. A screen matches a
     pattern in one company's own filings and cannot reason that a
     closed shipping lane raises a freight rate, that the rate squeezes
     an importer, and that the importer's domestically-sourced
     competitor gains. The method the prompt asks for is: start from the
     cause, name the company and the mechanism in one line, **then find
     the dated event** — a consequence is not a catalyst — then cite
     what you found. "Two or three links is reasoning. Five is
     astrology." It had billed **zero** web searches across 18 calls
     before this, because it had never been offered the tool.
3. **Claude researches** each candidate: given the live price, the move
   since the catalyst, volume, range position and three years of the
   stock's own history, plus its own web searches. It returns a
   direction, a calibrated conviction, a thesis, an invalidation, an
   expected holding period, and whether it judges the move already
   priced in.
4. **Deterministic code sizes and places.** Whether the conviction
   clears the floor, how large, where the stop sits, when it exits.
5. **Claude re-reads open positions**, and may bring an exit date
   forward — never push one out.

Both candidate sources go through the identical research, pricing, risk
and execution path. Nothing downstream knows which found it. They are
stamped with their origin so the record can eventually say which is
worth the money.

---

## The one rule that is not negotiable

**The model proposes, deterministic code disposes.** Claude decides
*what is worth trading and whether*. Code decides *how much, and where
the stop sits*. The model never sizes a position or places an order.

`risk/sizing.py` does not merely avoid reading conviction — it has no
parameter a model-supplied number could arrive through, and a test
holds that shape.

**Why, in one example from this bot's own record:** a candidate scored
**0.82 conviction** on a genuinely compelling, well-argued thesis. The
thesis concluded *do not trade*. Persuasiveness and correctness are
different properties, and a model that sizes its own positions converts
the first into money.

This is the rule the owner has affirmed repeatedly. It is not a
limitation on Claude's intelligence; it is the reason a wrong answer
costs one position instead of the account.

**Open, and gated on evidence:** letting conviction *scale* size within
the hard bounds — code still computes it, conviction becomes an input.
That requires conviction to be demonstrably calibrated first. It is not
yet. See "What is not proven".

---

## Fixed constraints

- Trading capital: **$2,000**, paper account until proven.
- **No Pattern Day Trader rule.** Retired 4 June 2026 — unlimited day
  trades at any account size. But margin needs $2,000 and **leverage is
  not used**: measured, the bot deploys at most 22% of available buying
  power, so borrowed money would change nothing except the downside.
- Hold **days to weeks, never months.** Hard exit date on every position.
- **Three to five positions**, genuinely uncorrelated. Four biotech
  binaries resolving the same fortnight is one bet, not four.
- Runtime API budget: **owner-set, currently $100/month.**

### The budget arithmetic, stated plainly

Costs are near-fixed in cash terms, so against a small account they are
punishing:

```
 $5/mo  =  3% annual hurdle
$10/mo  =  6%
$20/mo  = 12%
$40/mo  = 24%
$100/mo = 60%   <- current setting
```

**At $100/month on $2,000 the bot must clear roughly 60% a year to
match holding cash.** The S&P long-run average is about 10%. That is the
number every discussion of "is this working" has to start from, and
lowering the cap is the single cheapest improvement available.

Measured cost per research call: **~$0.19 average, $0.45 worst**, driven
almost entirely by web-search results arriving as input tokens
(34k median, 166k max).

### Throttles derive from the budget, never from a constant

Raising the cap raises what the bot does, with no second number to
remember:

| monthly cap | daily ceiling | research/cycle | hunts/day |
|---|---|---|---|
| $20 | $5.00 | 3 | 0 |
| $100 | $10.00 | 6 | 2 |
| $300 | $30.00 | 12 | 4 |

Floors are the owner's own earlier figures, so lowering a cap can never
strangle the bot below what was already agreed.

---

## Every number that touches money comes from a tool, never the model

This is the "validate its findings" half, and it is enforced
structurally rather than by instruction:

- **Price** — the mid of Alpaca's live NBBO, refused if older than ten
  minutes, non-positive or crossed. Then **cross-checked** against the
  newest cached daily close: beyond ±35% it is flagged and shown;
  beyond 5x it is refused and no order is placed.
- **Volatility and gap** — measured from that ticker's own three years
  of daily bars, not a category guess. Per-stock evidence may only ever
  *tighten* a category assumption, never loosen it.
- **Fills** — whatever the broker reports, recorded verbatim beside any
  modelled figure, never instead of it.
- **Claude's submission tool has eight fields and none of them is a
  number that touches money.** A "$35" read off an article can only
  land in free text, which no arithmetic reads.

**What is NOT validated:** anything Claude reads in a web search. That
can move direction, conviction and priced-in — so a wrong source can
cause a *wrong* trade, but never a *wrongly sized* one.

---

## Conviction is a frequency, not a feeling

The field had no definition at all until 2026-08-17, and it cost every
trade: eight longs scored between 0.30 and 0.45 against a floor of 0.60.
Not disagreement — two scales never reconciled.

It is now specified as **how often this call would be right across many
similar setups**: 0.50 a coin flip, 0.60 six in ten, 0.75 three in four.
Below 0.50 on a direction is a contradiction and should be `no_trade`.

**The floor is never named in the prompt or the tool.** Telling the
model the bar teaches it to clear the bar, which turns the only
measurement worth having into a formality. A test reads the live floor
and asserts it never leaks.

---

## Thresholds are measured, not asserted

Conviction floors, gap assumptions and stop widths start as estimates
and must adapt on **closed, scored outcomes** — never on projections or
the model's own confidence.

But **hard bounds never move by themselves** — max loss per position,
total exposure, max positions, the daily-loss and drawdown kill
switches. Those prevent ruin; the system may propose changes, a human
decides. Tighten fast on evidence of harm, loosen slowly on evidence of
over-caution, and log every adjustment with the evidence behind it.

The refusal tracker is the main feedback loop: record the price when a
candidate is declined, then score what it went on to do.

### What the owner decided on 2026-09-05, and the evidence behind it

*"The bot isnt aggressive ... i dont really need any hard limit except
a hard stop to stop bot using all the budget ... optimize heavily to
ensure ... claude can make profitable trades and multiple times a
month."* From their own 7-day bundles: 33 candidates researched, 31
declined as priced in, 2 longs at 0.56 and 0.57 under a 0.60 floor, the
drift arm producing nothing, and the bot **paused for 3.5 of the 7
days** by a cost discrepancy that was a pricing forecast's fault.

- **No pause but the budget.** The reconciliation records a discrepancy
  with its reason and corrects the rate from the bill; it never gates
  spending again. The governor keeps one integrity gate — an unpriced
  row, which is a hole in the count the budget stop needs — and the
  monthly and daily caps. That is the whole list.
- **Conviction floor 0.60 → 0.50.** One long call in twenty-one ever
  cleared 0.60, and the floor's own evidence sample (refusals refused
  *for* the floor) was 15 against a 30 minimum, so it could never have
  moved on its own. 0.50 is where the definition already draws the
  line. The tracker can raise it again on evidence, three times faster
  than it lowers.
- **The drift arm's universe is every 10-Q/10-K filer**, from the same
  daily index the Form 4 feed already downloads, not the companies
  insiders happened to trade. Filers are fetched first. The research
  prompt has a drift brief: a stock that has already moved on a beat is
  confirming the setup, not exhausting it.
- **Hunts 2/day at $100.** The two live days spent $1.33 and $6.69 of
  a $10 ceiling with research slots to spare — candidate supply was the
  constraint, not the budget to judge it.
- **Daily position review was already daily** (`REVIEW_INTERVAL_HOURS
  = 24`, brought forward by news). It ran zero times that week because
  the only position was past its exit date and the pause blocked
  everything else; there was nothing to fix there.

**Kept, and why:** the hard bounds — 2% max loss per position, five
positions, the daily-loss and drawdown kill switches. None of them was
stopping a trade; they only bound how big a trade is. They stay the
owner's decision.

### What 2026-09-11 measured, and what moved

Everything above shipped and is working. The 09-11 bundles show
denials with `reconciliation_discrepancy_unacknowledged` stopping dead
at 2026-09-05T00:15 followed by 89 allows; the drift arm producing 32
candidates where it produced none; the hunt producing 8 forward-dated
candidates; **69 research calls over four trading days** against 33 in
the whole previous week; $21.25 of $100 spent; reconciliation agreeing
to the cent on every closed day.

**The blocker moved rather than persisting.** 69 researched produced
four directional views, and all four died:

| | | |
|---|---|---|
| CASY | short 0.58 | cash account cannot short |
| COO | short 0.60 | cash account cannot short |
| BWFG | long 0.55 | spread gate: half-spread 99.2bp vs a 20bp bound |
| UBER | long 0.62 priced_in | needed 0.65 |

**Two of the four are correct and stay correct.** BWFG is a microcap
bank whose half-spread measured 99.2 basis points — a ~2% round trip
against an 8% assumed move — and that is a hard bound, so it is the
owner's to change; on this evidence it is right. The shorts are a cash
account being a cash account.

- **Priced-in premium 0.15 → 0.05.** On a 0.50 floor, 0.15 is a 0.65
  bar. `priced_in` is set on nearly everything — 62 of 65 no_trades
  and 1 of 2 longs — because for a public filing days old the honest
  answer usually is "partly". But conviction is *defined* as a
  frequency: a model that thinks half the move is gone says so by
  scoring 0.62 rather than 0.80, and charging 0.15 again double-counts
  it. Not zero: a priced-in long now needs 0.55, so UBER trades and a
  0.52 priced-in long still does not.
- **The research prompt says the account cannot short.** Two of four
  paid directional views were shorts the engine must discard. Phrased
  as a fact about the account, never as pressure toward `long` — a
  bearish read is still the right answer, recorded as `no_trade` with
  the bearish case in the thesis, which the refusal tracker scores.
  What was wasted was the search budget spent *building* a case that
  cannot be acted on.
- **The hunt gets web_search and a second-order brief** (see "What the
  bot actually does" above). The chain the owner asked for — war →
  supplier → beneficiary — cannot start in a filing, and the hunt had
  no tool that could see outside one.
- **Unscored refusals now say why.** 291 refusals, essentially none
  scored, and nothing anywhere saying what they were waiting on. The
  reason was held in a module-level dict, which no diagnostic bundle
  can carry — the bundle is written by a different process. It is a
  side table now (`refusal_scoring_skips`), with an attempt counter, so
  "the quote failed once" and "this ticker has refused forty times" are
  different facts.
- **The SPY health probe retries a transient answer.** The owner's
  "reachable, but no feed returned a SPY bar ... HTTP 504" was one bad
  second reported as a verdict, while the bot read bars happily because
  `refresh_benchmark` retries and the probe did not. Three attempts on
  429/5xx; **not** on 401/403, which are the entitlement and never fix
  themselves.

**Recorded, not changed: the conviction floor at 0.50 refuses nothing
by construction.** The prompt's own scale says "Below 0.50 on a
direction is a contradiction", so a directional view under 0.50 is one
the model is instructed never to submit. The owner's week bears it out:
lowest directional view 0.55, `below conviction floor` fired zero
times. That is what "the only stop is the budget" means, and the real
bounds are the hard bounds and the priced-in premium — but it is
written down because a threshold that looks like it works and refuses
nothing is exactly the shape the adaptive table exists to prevent. The
floor can only start mattering again by being *raised* above 0.50 on
scored evidence. Treat a zero count of `below conviction floor` as the
expected reading, not as good news.

### Where the research budget goes, measured 2026-09-11

The owner asked for more trades. The funnel says the budget was being
spent on arms that have never produced one. Per arm, from the 7-day
window joined to the pricing bundle:

| arm | paid calls | spend | $/call | directional views, lifetime |
|---|---|---|---|---|
| conjunction | 33 (48%) | $9.15 (58%) | $0.277 | **0 of 89** |
| insider (`screen`) | 23 (33%) | $4.10 | $0.178 | 23 of 189 |
| earnings_drift | 13 (19%) | $2.45 | $0.189 | 0 of 13 (2 shorts) |
| hunt | 0 | — | — | 0 of 2 |

**There was no judgement behind that split.** `fresh[:max_research]`
took the first six of a list built insider → conjunctions → drift →
hunt, so allocation was by **list position**, and conjunctions emit
across fifteen catalyst types. The most prolific builder wins the
budget. So the dearest arm per call took nearly half the calls for a
lifetime record of zero tradeable views, while the best-graded arm got
thirteen and the hunt got none.

- **Research slots rotate across arms**, one per arm per round, ordered
  `earnings_drift → hunt → screen → conjunction`. Nothing is discarded:
  a candidate pushed past the belt returns next cycle exactly as before.
- **An arm with no directional view in 40+ paid calls gets a probe
  share** — one round in four, never zero, so it keeps generating the
  evidence that would restore it. 40 is the stated minimum: at the
  insider arm's measured 12.2% conversion, zero views in 40 calls has
  probability 0.878⁴⁰ ≈ 0.6%. The hunt at 2 calls is **not** demoted;
  it is new, not proven bad. The demotion is a reading of the record
  every cycle, never a stored flag.
- **`CONJUNCTION_SEARCHES` 10 → 3**, the base allowance. The extra
  seven were justified by "the answer lives in reporting the feeds do
  not carry". After 89 calls at the larger allowance and no view, they
  are not the missing ingredient, and `searches_for` already states the
  rule: evidence buys budget, never hope.
- **Conjunctions are stamped as their own origin.** They shared
  `screen` with insider clusters, so no arm could be held to its own
  record — which is why this was never visible before.

**Next decision, gated on evidence:** if conjunctions produce no
directional view over the next measured window, unwire the arm as
`etf_rotation` already is.

### The budget is fully committed — corrected 2026-09-11

$21.25 of $100 month-to-date reads like headroom and is not. Four of
the month's first days were the pause. On the four days the bot was
actually active it spent **$13.98, or $3.50/day, against the $3.33/day
the $100 cap sustains** — 105% of the rate, projecting to $105/month.

So raising `research_per_cycle` was considered and **rejected**: it
would front-load the month and then go dark, which is the exact failure
`DAILY_BURST_DAYS` exists to prevent, and it would make the live record
incomparable to a backtest that trades every day. The 137 deferrals in
the window are the belt spreading a day's affordable calls across
cycles, which is its job.

**The consequence is what matters: there is no spare money to grow
into.** Every call on an arm that does not convert displaces one that
might, so allocation and cost per call are the only levers left. Both
were pulled above; the same $15.70 buys roughly 88 calls at the insider
price instead of 69 at the old blend.

### Conviction was two scales sharing one column

Measured across all 293 research views:

- **268 no_trade** — conviction median 0.68, 97 of them at 0.80 or
  above, max 0.85.
- **25 directional, ever** — every single one between 0.30 and 0.62.

The tool asked for one field and said that on a `no_trade` it is "your
confidence that NOT trading is correct, **judged the same way**". It is
not the same way. A directional conviction is a frequency over market
outcomes, where 57% out of sample is the best this project has ever
measured. A `no_trade` conviction is self-certainty about an
abstention, which is nearly free to feel strongly about. Sharing a name
and a column, the scale read as though declining were the confident
answer and committing the weak one — so an honest 0.56 long looked
like a shrug beside a 0.85 `no_trade`, and **91.5% of every paid call
went the comfortable way.**

This is the 2026-08-17 defect surviving in the other branch: that date
defined the directional scale as a frequency and said nothing about
what the number means when there is no direction to be a frequency of.

- **The tool says the two are different quantities and not
  comparable**, that being sure there is nothing here is cheap, and not
  to reach for `no_trade` because it is the branch where you can score
  highly. It still forbids inflation in the other direction.
- **The prompt anchors the scale on this project's own out-of-sample
  hit rates** (57% and 49%) and says plainly that whether a number is
  big enough is computed downstream by code the model cannot see.
- **The record labels each remembered number** as a directional
  frequency or a confidence in declining. Once refusals start scoring
  it would otherwise have shown the model a track record in which
  declining always scored higher than committing.

**The floor is still never named**, and the test that holds that still
stands. Calibration evidence is allowed; the bar is not.

---

## What is not proven

Kept here deliberately, because a spec that only describes intentions is
how a bot ends up looking finished while doing nothing.

- **It has never traded.** Zero orders to date. Every claim about
  whether any of this works is untested in the only way that counts.
- **The backtest graded the mechanical screen, not Claude.** All 23
  backtest runs are `mode='structural'` — a replay with no model in the
  loop. `backtest/judgement.py` is a three-line stub. Nothing has ever
  measured whether Claude's judgement adds value, and a backtest largely
  cannot: the model already knows what happened to any stock before its
  training cutoff.
- **The graded configuration underperforms SPY** once real costs are
  applied. Out-of-sample, insider-cluster: +31.6% excess with no API
  cost, +6.7% at $8/month, −15.2% at $8/month with 30bp/side.
- **Claude now sees its own record, and it is one trade long.** Since
  2026-09-05 the research prompt carries the closed trades and the
  scored refusals (`research/record.py`): what earlier calls went on to
  do, as text the model weighs and no code reads back. Whether being
  shown a record improves the next call is itself unmeasured.
- **The conviction scale is newly defined and uncalibrated.** Whether a
  0.6 call really resolves six in ten is unknown.
- **Two of the three candidate arms have never produced a tradeable
  view.** Conjunctions: 0 directional views in 89 paid calls, and never
  backtested at all. Earnings drift: 0 in 13, and its only two
  directional views were shorts a cash account cannot take — so the
  best-GRADED arm has produced nothing this account could act on. Every
  order the bot has ever considered came from insider clusters, the arm
  that graded worst out of sample (49.3%, 41.2% max drawdown).
- **Whether the conviction anchor helps is unmeasured.** Telling the
  model what 57% means in this domain is meant to stop it retreating
  from an honest modest edge. It could equally teach it to cluster
  around the number it was shown. The refusal tracker is what would
  tell the difference, and it has scored nothing yet.
- **The main feedback loop has produced no evidence yet.** 291 refusals
  on record and essentially none scored as of 2026-09-11. Since the
  brief calls the refusal tracker "the single most important feedback
  loop in the system", every adaptive number in the table is still
  sitting on its estimate — including the two that were moved by hand
  on 09-05 and 09-11. The tracker now says *why* each refusal is
  unscored, which is the prerequisite for fixing it, not the fix.

---

## Runtime

Ubuntu VPS, systemd, unattended. Dashboard on port 8000, bound to
0.0.0.0, protected by an access code the installer generates. The VPS is
IP restricted. Credentials never in the repo.

## Not optional

- **One-command install, and a UI for entering credentials.** Nobody is
  ever told to edit a config file.
- **Every trade must be explainable after the fact** — what the model
  saw, what it concluded, what the risk engine did, what happened.
- **Logs searchable from the dashboard.** No SSH required to troubleshoot.
- **A zero is never left unexplained.** Print the raw upstream response
  beside any empty result.
- **Routine attrition must not look like damage.** Drop reasons are
  tagged routine / a limit / fault, and only the last deserves
  attention. A working bot reading as a broken one has cost real
  debugging time twice.

## What the owner wants back: results, not permission

Owner-set 2026-08-31: *"i just want you to give me results"*.

- **Decide it, do it, then say what happened.** Do not present options
  and wait. If a call is genuinely finely balanced, make it, say which
  way you went and what would reverse it.
- **A question to the owner is a last resort**, not a courtesy. The
  only thing that genuinely needs them is a hard bound (house rule 5).
- **Lead with the outcome.** What is fixed, what shipped, what commit
  is on `main`, and what it means for the money. The reasoning goes
  underneath for whoever wants it, and in the commit message for
  whoever comes next.
- **Never report a fix you have not confirmed landed** (house rule 2)
  — moving faster is not licence to be vaguer. "It should be fixed" is
  still not a report.

## Avoiding collisions

Agree interfaces before parallel work. One owner per file — see the
ownership table in the brief. Branch per task, merge one at a time, run
the tests between merges. Schema and config changes go through a single
session, never two at once.

**Never add a column to a hot table.** `candidates`, `orders`,
`limit_applications` and `fills` are written with positional INSERTs in
many places; a new column silently shifts every one of them. Use a side
table — `candidate_origin`, `entry_market_context`,
`limit_application_notes` and `quote_cross_checks` all exist for this
reason. Learned by doing it the other way and breaking 157 tests at once.

## Finished work goes to `main`, or it does not exist

**The VPS follows `main`.** `install/upgrade.sh` runs `git pull
--ff-only`, so work sitting on a feature branch is invisible to the
owner no matter how well it is tested.

This has already cost an evening: six commits of finished, green work
sat on a branch while the owner ran the upgrade, was told "Upgrade
complete", saw the same version before and after, and went hunting
through browser caches for a change that had never reached the machine.

So, whenever you say a change is ready for the owner to upgrade:

1. **Land it on `main` first**, then tell them.
2. **Say which commit is on `main`**, so "did it ship" is a check rather
   than a memory.
3. **Confirm, do not assume** — `git log --oneline origin/main -1` and
   `git log origin/main..HEAD` (the second must be empty).
4. **There is no exception.** Money-critical code lands the same way,
   once house rule 5's gate is satisfied. The old carve-out — open a PR
   and wait on the owner — is gone as of 2026-08-31, at their
   instruction; the only thing still theirs to decide is a hard bound.

The version string is not the signal — it is hand-maintained and sits
still across real changes. The **commit** and the dashboard **build
hash** are what move, and `upgrade.sh` prints both.

## Commands

- Run all tests before any commit. They must be **fully offline**.
- Never edit `.env`. Never commit credentials.

## House rules

1. Verify by running it. An asserted fact is not a checked fact.
2. Never report a fix you have not confirmed landed.
3. Every zero gets its raw upstream response printed beside it.
4. A test that cannot fail is not a test — break a copy, confirm it
   catches it.
5. **Money-critical code reviews itself. It does not wait for the
   owner.**

   Every file that sizes, stops, orders, reconciles or crosses the
   model/code boundary carries a literal `MONEY-CRITICAL` marker, and
   `test_scaffold.py` holds that the marker is there — so the list is
   greppable rather than remembered.

   Changing one of those files requires all four of these **before it
   lands**, and nothing else:

   - **An adversarial read, written down.** `risk-reviewer` is the
     right tool where a subagent is available (it has no write tools by
     design, so it can never be the thing that breaks). Where it is
     not, the read still happens and its answers go in the commit or PR
     body — the gate is the written answers, not who produced them:
     *what is the worst input this now accepts? what does it do when
     the broker lies, times out, or answers half? can it place, size or
     cancel anything it could not before? what happens on the second
     call, and on the retry?*
   - a test that fails against the old behaviour — sabotage a copy and
     watch it go red (house rule 4).
   - the full suite green, offline.
   - the commit message saying what the evidence was.
   - **a row in @docs/WHAT-WE-TRIED.md** (house rule 8), in the same
     commit, carrying the measurement rather than the argument.

   **Owner-set 2026-08-31**, replacing "changes to risk, execution or
   broker code need human review": *"merge all, change rules so you
   dont want me everytime, i just want you to give me results"*. The
   old rule was costing more than it caught — finished, green fixes sat
   on branches waiting for a person while the bot kept running the
   broken version. The EMBC exit failed roughly 190 times across two
   days partly for that reason.

   **THE ONE EXCEPTION, AND IT DOES NOT MOVE.** Hard bounds — max loss
   per position, max total exposure, max positions, the daily-loss and
   drawdown kill switches. Those exist to prevent ruin, not to be
   correct. The system may *propose* a change to one and must never
   make one; that decision stays with the owner, and it is the only
   thing that still does.
6. **Never anchor a test to a calendar date** when the code it tests
   measures against `datetime.now()`. The fixture drifts out of the
   window a day at a time and the suite goes red for a reason unrelated
   to what it tests. This has happened twice.
7. **Classify by the rule, not by enumeration.** A hand-written list of
   known cases mislabels the first case nobody thought of — three
   separate owner reports came from exactly that.
8. **Every change that lands appends to @docs/WHAT-WE-TRIED.md.**

   Owner-set 2026-09-11: *"i want a massive sheet so you can reflect
   back on it and slowly understand actions we've taken and build from
   it. A doc you can reference so it can help you with your memory"*.

   A session has no memory of the last one, so "I will remember to
   record this" is the one promise that cannot be kept by intending it.
   The doc is the memory, and it is only worth anything if it is
   current. So the row goes in **as part of landing the change**, in the
   same commit, not afterwards:

   - what was tried, and **what the evidence was** — the measurement,
     not the reasoning.
   - what happened. **A thing that did not work is the more valuable
     row**, because it stops the next session spending money to learn it
     again.
   - if it overturns something the doc already claims, mark the old
     entry `SUPERSEDED` and say by what. Never delete it — losing the
     fact that we once believed it is how a wrong belief comes back.

   This is also the fourth item on house rule 5's gate. `tests/
   test_the_memory_doc_survives.py` holds that the doc exists, that this
   file still references it and that its sections are intact — it
   **cannot** detect that the doc is stale, which is why this is a rule
   and not only a test.
