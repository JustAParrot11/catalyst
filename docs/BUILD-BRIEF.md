# Build brief — autonomous catalyst trading bot

You are building this from scratch. The architecture is yours. This brief
gives you the goal, the fixed constraints, and a short list of things
that have already cost real money to learn — nothing more.

---

## What we are building

An autonomous trading bot that finds scheduled market catalysts,
researches them, and trades the ones with genuine edge — unattended.

**Claude makes the judgements. Deterministic code makes the decisions.**
The model researches a candidate and returns a view. Code decides whether
to trade, how large, and when to exit. The model never sizes a position
or places an order directly.

This one piece of architecture is not up for debate. It is what makes the
system auditable, and what stops a persuasive but wrong thesis becoming a
large position.

---

## Stack

- **Broker:** Alpaca. Paper account only until performance is proven.
- **Intelligence:** Claude via the Anthropic API, for research and judgement.
- **Data:** free and keyless sources wherever possible. Paid search is a
  last resort, never a default.
- **Runtime:** Ubuntu VPS, systemd service, running unattended.
- **Dashboard:** served on port 8000, bound to 0.0.0.0. The VPS is IP
  restricted, so the dashboard does not need to defend itself against
  the open internet. Credentials still never go in the repository —
  that is about the repo being shared, not about the network.

---

## Hard constraints

| Constraint | Value |
|---|---|
| Trading capital | **$1,000, fixed** |
| Development budget | **$200 one-off**, for building only |
| Runtime API budget | **£20/month absolute ceiling** — aim well below |
| Live trading | **paper only** until proven |

**The £200 is for building. It is not a monthly allowance.**

The runtime ceiling is **£20/month, and that is a ceiling rather than a
target.** Costs are near-fixed in cash terms, so against $1,000 they are
punishing:

```
 £7/mo  =  $8.89/mo   =  10.7% annual hurdle   (what a previous build actually cost)
£12/mo  = $15.24/mo   =  18.3% annual hurdle
£20/mo  = $25.40/mo   =  30.5% annual hurdle   (the ceiling — a hard bar)
```

The S&P long-run average is roughly 10% a year. **At the ceiling the bot
must beat about 30% a year simply to match holding cash.** Every pound of
runtime spend has to be earned back before a single trade counts as good.

A previous build measured $0.415/day of scheduled spend — around £7/month
— running one discovery pass and one analysis daily. That is the realistic
shape of it, and it is where you should aim. Treat anything above £12 as
needing a specific justification from the backtest.

Here is the fuller arithmetic:

```
$5/month  =  6% annual hurdle   workable
$8/month  = 10%                 workable
$20/month = 24%                 must beat 2x the S&P average to break even
$36/month = 43%                 not viable
```

Whatever figure you choose, state what annual return the strategy must
clear to justify it, and show that the backtest supports that return.
A high runtime cost is defensible if the edge is real and large; it is
indefensible on a hope.

**Use as many agents as you like to build this. Ship something lean.**
Every model pass in the live path multiplies the running cost — a
challenger-and-judge architecture over eight candidates a day is roughly
$50/month, a 60% hurdle. Build rich, run cheap.

**Cost governor.** Expected profit must never authorise spend. Base cap
$5/month, hard. It may rise only by a fraction of *realised* profit from
closed trades — never on projections or unrealised gains. If a cycle
would breach the cap, it skips and reports that it skipped.

---

## Trading behaviour required

These are requirements, not preferences.

**Hold days to weeks, never months.** Every position carries a hard exit
date set when it is opened. If the thesis has not played out by then, the
position closes regardless — a trade held for months is capital doing
nothing while still carrying risk, and it stops the sample growing.
Target a typical hold of days to about three weeks.

**Several positions at once, genuinely uncorrelated.** Aim for three to
five open at a time. No single position should dominate the account.

Critically: **correlated positions are one bet wearing several hats.**
Four small-cap biotech binaries all resolving the same fortnight is a
single wager on biotech sentiment, not four independent trades. The risk
engine must recognise concentration by sector, catalyst type and
resolution date — not just by ticker count.

**Enough trades to learn from.** A strategy producing one trade a month
cannot be evaluated within a year. Frequency is a design requirement,
because a sample you never accumulate teaches you nothing. If a strategy
cannot produce a few trades a month at acceptable risk, say so early
rather than waiting six months to find out.

## What to build

1. **Data collection** — as many free structured sources as you can find.
   Breadth should cost nothing per item.
2. **Discovery** — turn raw data into dated, tradeable candidate events.
3. **Research** — Claude investigates a candidate and returns a structured
   view: direction, conviction, thesis, what would invalidate it, expected
   holding period, and whether it is already priced in.
4. **Risk engine** — deterministic only. Sizing, exposure limits, stops,
   kill switches.
5. **Execution and management** — orders, stops resting at the broker,
   fill reconciliation, time-based exits.
6. **Cost tracking**, accurate to the cent. Harder than it looks; see the
   traps. Getting it wrong hides the number that decides viability.
7. **Dashboard and UI** — see below.
8. **Backtest harness** — build early, see build order.

---

## The system must tune itself — within bounds that never move

Every threshold in a trading system is somebody's guess until it is
measured. A previous build shipped with an assumed 60% adverse gap and a
0.65 conviction floor, both invented, neither validated. If those numbers
are wrong the system refuses good trades forever and **never signals that
it is doing so.** That silence is the defect, not the numbers.

So the parameters must adapt on evidence. But an unconstrained adaptive
system is how automated accounts die: a lucky run loosens the limits, the
larger positions meet an unlucky run, and the account is gone. Both
failure modes are real and the design must defeat both.

### Two tiers, and only one of them moves

**Hard bounds — never adjusted by the system, at all.** Maximum loss per
position, maximum total exposure, maximum positions, the daily-loss and
drawdown kill switches. These exist to prevent ruin. The system may only
ever propose changing them; a human decides. If an adaptation would
breach one, it does not happen, and the dashboard says which bound
stopped it and by how much.

**Adaptive parameters — moved by measured evidence.** Conviction
threshold, assumed adverse gaps per catalyst type, stop widths, holding
periods, how much search budget goes where. These start as estimates and
should not stay that way.

### The rules adaptation must follow

1. **Closed, scored outcomes only.** Never unrealised P&L, never
   projections, never the model's own confidence.
2. **A minimum sample before anything moves**, per parameter. Adapting on
   four trades is fitting noise, and noise is what you are trying to
   remove. State the minimum and defend it.
3. **Asymmetric speed.** Tighten quickly on evidence of harm; loosen
   slowly on evidence of over-caution. Getting cautious too fast costs
   opportunity; getting aggressive too fast costs the account.
4. **Bounded step size.** No parameter moves more than a small fraction
   per adjustment, however emphatic the evidence.
5. **Every adjustment is logged with the evidence that caused it** — the
   old value, the new one, the sample it rested on, and what would
   reverse it. Visible on the dashboard, not buried.
6. **Reversible.** Any adjustment can be rolled back, and the system
   reverts automatically if the change makes results worse over the next
   sample.

### Where the evidence comes from

- **The refusal tracker.** Record the price whenever a candidate is
  declined, then score what it went on to do. If refused candidates are
  systematically profitable, the threshold that refused them is too
  strict — and now that is a number rather than an argument. This is the
  single most important feedback loop in the system.
- **Closed trades**, by catalyst type and by rule: which limits bound
  most often, and whether the ones binding were the ones that mattered.
- **The backtest**, re-run against the accumulating live record to check
  the live results still resemble the tested ones.

### The honest constraint

At a few trades a month, meaningful samples take months. Adaptation will
be slow, and any design that appears to learn fast is fitting noise.
Build it so it is *correct* rather than *responsive*, and say plainly on
the dashboard how far it is from having enough evidence to move anything.

## Installation and setup — assume no technical knowledge

The owner is not a developer and must never be told to edit a config
file.

**One command installs it.** A single script: dependencies, virtual
environment, systemd service, started. It verifies each step and, on
failure, says what failed and what to do about it. Safe to run twice.

**A setup UI collects the credentials.** On first run the dashboard shows
a form: Alpaca keys, Anthropic key, settings. Every field explained in
plain English. A "test connection" button beside each that reports
success or the real error.

Credentials are written to a file readable only by the service user, and
are **never logged, never shown again once saved, and never included in
any diagnostic output**. Redact at capture, not on the way out.

**One command upgrades it**, backing up the database and config first,
running the full test suite after, and rolling back automatically if the
tests fail.

## Dashboard — make it genuinely useful

The dashboard is how a human decides whether to trust the machine.

- **Every number says where it came from** — billed or estimated, which
  window, how many samples.
- **A zero is never left unexplained.** Print the raw upstream response
  beside any empty result. "No data" and "the query is broken" look
  identical otherwise, and telling them apart is repeatedly the whole
  diagnosis.
- **Show the funnel** — raw candidates → screened → researched → proposed
  → orders placed, with the drop reason at each stage. When it has not
  traded, the dashboard should name the stage responsible.
- **Show cost honestly** — separate scheduled running cost from manual
  testing, and express it as an annual hurdle against the account, not
  just a monthly figure.
- **Compare against the S&P and T-bills**, net of API costs, exposure
  matched. Paper P&L is fictional; the API bill is real money.
- **Show refusals and what they went on to do.** Record the price when a
  candidate is declined, then check the outcome later. That turns "is it
  too strict?" from an argument into a number.
- **Never present an early number as a verdict.** Say when the sample is
  too small to mean anything.

### Show why every trade happened

For each trade — taken *or declined* — the dashboard must be able to
reconstruct the whole decision:

- **What the model saw.** The candidate, the data it was given, every
  tool it called and what came back. If it searched, the queries and the
  sources.
- **What it concluded**, in its own words: the thesis, the conviction,
  what would invalidate it, whether it judged the move already priced in
  and on what evidence.
- **What the risk engine then did.** The position size, every limit that
  bound it and by how much, the stop and why it sits there. Where the
  code overruled the model, that must be visible and explained.
- **What actually happened.** Fill price against intended, the exit and
  its trigger, realised P&L against expectation.

Present it as a readable narrative, not a log dump. The test: **someone
who was not there can read a single trade and understand why it was
made.** If a trade loses money, this view is how you find out whether the
reasoning was wrong or merely unlucky — and those need opposite
responses.

### Logs you can actually troubleshoot from

- Searchable and filterable in the browser by level, time, and component.
  Nobody should need to SSH in to read a log.
- Every model call recorded: prompt, response, tools used, tokens, cost,
  latency.
- Errors carry the full traceback and the state at the time.
- One click exports a diagnostic bundle for sharing — **with credentials
  redacted**.

---

## Free data sources

Verified working, keyless unless noted:

- **Federal Register API** — agencies must publish advisory committee
  meetings and scheduled actions in advance, with dates. Every agency,
  not just FDA.
- **SEC EDGAR full-text search** (`efts.sec.gov`) — proxy filings carry
  shareholder meeting dates. Requires a contactable User-Agent.
- **ClinicalTrials.gov API v2** — late-stage study completion dates.
- **Alpaca market data and news** — already paid for in the subscription.
- **openFDA** — historical approvals and Complete Response Letters. Base
  rates only; see traps.

Find more. Breadth from free structured APIs is nearly free. Breadth from
web search is not.

---

## Traps that have already cost money

Short list, all learned the hard way. Everything else is your call.

**Cost tracking**
- The Anthropic Cost API reports **whole days only**. Today's spend is not
  queryable until the day closes — a naive reading shows $0 and looks like
  a broken account.
- Amounts are decimal strings in **cents**, not dollars.
- **Cache tokens are billed but are not in `input_tokens`.** Capture
  `cache_read_input_tokens` and `cache_creation_input_tokens` explicitly.
  Missing them understates the bill by about half. Cache writes cost
  **1.25x** input; reads **0.1x**.
- Store the **raw usage object** verbatim. Reading named fields means a
  renamed or nested one silently prices itself at zero.
- Default page size is small; a longer window needs an explicit limit or
  it quietly drops the newest days.
- **Web search costs $10 per 1,000 queries** on top of tokens. Omitting it
  understated a real run by 89%.
- Separate **scheduled** spend from **manual** spend, or every projection
  is wrong — alarmingly so in the first few days.

**Alpaca — the day trading rules changed in June 2026**
- **The Pattern Day Trader rule has been retired.** The SEC approved the
  amendment to FINRA Rule 4210 on 14 April 2026; it took effect 4 June
  2026 and Alpaca implemented it the same day. There is **no PDT
  designation, no day-trade counting, and no $25,000 minimum equity
  requirement**. Unlimited day trades on a $1,000 account are permitted.
  Any assumption otherwise is out of date and will wrongly rule out
  entire strategy classes.
- **The $2,000 minimum equity for margin still applies** — a separate,
  pre-existing rule. At $1,000 you can day trade without limit but
  **cannot use leverage**. Design for an unleveraged account.
- **Alpaca removed the PDT API fields.** `pattern_day_trader`,
  `daytrade_count`, `last_daytrade_count`, `daytrading_buying_power` and
  `last_daytrading_buying_power` were removed from the API in July 2026.
  Use `buying_power`. Code referencing the old fields will break.
- Fractional stop orders are supported, but only with
  `time_in_force=DAY` — they expire at the close and must be re-placed
  each session.
- Stop orders do **not** trigger outside regular hours. Overnight gap risk
  cannot be removed with stock alone.
- Paper fills pay no spread. Model the cost, but record it *beside* the
  broker's price, not instead of it — reconciliation compares against the
  real fill.
- The corporate actions feed is ~98% dividends and yields roughly one
  usable name a day.
- An empty symbol list on the news API is treated as a filter, not as
  "everything".

**Data**
- **openFDA cannot tell you a decision is coming.** Every endpoint is
  retrospective. PDUFA dates are not published by the FDA at all —
  companies disclose them.
- **EDGAR is rate limited to 10 requests/second across all its APIs**;
  an overrun causes a temporary IP block. It throws transient 500s —
  retry those with backoff, never retry a 4xx.
- Do not use `DEF 14A` as a blanket filter. It is the annual proxy and
  floods discovery with routine annual meetings.
- A ClinicalTrials.gov primary completion date is **not** an announcement
  date. Sponsors report topline results weeks or months later.
- Judge source freshness by **type**, not age alone. A Federal Register
  notice from 30 days ago is normal lead time for a scheduled event, not
  staleness.

---

## Strategy — this is yours to decide

**No strategy is prescribed here. Finding the best way to make money is
the work, and it is your work.**

Everything below is evidence from one previous attempt. Treat it as data
about what was tried, not as direction. If your analysis says the whole
approach was wrong, say so — that is a valid and useful finding.

### What was tried, and what happened

The previous build traded scheduled catalysts only: FDA decisions,
merger votes, clinical readouts, earnings. Over five days it researched
eight candidates and declined all eight. Zero trades.

The declines were mostly correct, and that is the interesting part:

- Catalysts with **known outcomes** — stock splits, confirmed earnings
  dates, fixed-ratio mergers — were already priced in. Demonstrated
  repeatedly, not assumed.
- Catalysts with **unknown outcomes** — clinical readouts, regulatory
  decisions — are where edge plausibly lives. But a 60% adverse gap
  meant a position large enough to matter risked 8-12% of a $1,000
  account in a single morning.

That attempt concluded that **edge and un-sizeable risk were the same
property of the same trades** — and never resolved it.

### What this does not tell you

It does not tell you that catalysts are a bad idea. It tells you that
*one* implementation of *one* catalyst approach, on a small account,
with a specific set of parameters, produced no trades in five days.

The entire space is open to you:

- Intraday and short-horizon strategies. **These were previously
  impossible and now are not** — see the PDT change below. That alone
  reopens most of the strategy space.
- Post-event drift rather than event anticipation.
- Statistical and mean-reversion approaches.
- Momentum, relative strength, cross-sectional ranking.
- Anything the data supports that the backtest can grade.

**Propose several. Grade them all. Keep what wins.** The only
requirement is that your choice is defended with backtest results rather
than reasoning, and that it satisfies the trading behaviour requirements
above.

## How the agents avoid conflicting

Agents do not coordinate by themselves. Two of them editing the same
file means one silently overwrites the other. Three of them designing
in parallel before the interfaces are agreed produce three incompatible
designs. What prevents this is process, and it is not optional.

### 1. Agree the interfaces before anything is built

One session, no parallel work: propose the module structure and the
function signatures between modules. Commit that. Only then split. This
is the step that makes everything after it safe.

### 2. One owner per file

Fill this in once the module structure is agreed, and keep it current:

| Area | Owner | Nobody else edits |
|---|---|---|
| backtest harness | `backtest-engineer` | yes |
| strategy definitions | `strategy-analyst` | yes |
| risk engine, execution, broker | **human review required** | yes |
| cost tracking | `cost-auditor` | yes |
| data feeds | `data-engineer` | yes |
| dashboard and UI | `ui-designer` | yes |
| install, config, deploy | `integration-engineer` | yes |
| tests | `test-writer` and `stress-tester` | shared, by file |

Shared files are the danger. Database schema and configuration are
touched by everyone — route every change to those through a single
session, one at a time.

### 3. Branch per task, merge one at a time

Every agent works on its own branch and opens a pull request. Merge one,
run the full test suite, then merge the next. Never merge two branches
that touch the same file without re-running the tests in between.

### 4. Read-only agents cannot conflict at all

`risk-reviewer` and `market-structure` have no write tools by design.
They can always run in parallel with anything, safely.

## Choosing models

Claude Code lets each subagent specify its own model. Use that
deliberately — it is the difference between a fast cheap build and an
expensive slow one.

**Opus** for work where a wrong first answer is expensive and hard to
detect: strategy design, architecture, risk reasoning, anything
involving subtle statistical judgement.

**Sonnet** for everything with a clear right answer and a fast feedback
loop: implementation, tests, refactoring, data plumbing, UI. Most of the
build is this.

**Haiku** for bulk mechanical work — searching, summarising, formatting —
where the task is simple and the volume is high.

The starting agent definitions set this per specialist. Change them if
your experience differs; the frontmatter `model:` field is yours.

One caveat: each subagent runs as its own instance with its own context,
so a team of three consumes several times the tokens of a single
session. Parallelise where the work is genuinely independent, not by
default.

## Build order

1. **Backtest harness first.** Nothing else can be graded without it.
   Replay historical catalysts against real price history: do these events
   have edge at all, what adverse moves actually occur, where should
   thresholds sit? It costs nothing to re-run and answers in an afternoon
   what forward testing needs six months to answer.
2. **Cost tracking and the governor**, so spend is visible from day one.
3. **Strategy bake-off** — several agents propose variants, all scored on
   the same backtest. Keep what wins.
4. **The pipeline** around the winning strategy.
5. **Dashboard**, in parallel with 3 and 4.
6. **Forward paper trading**, with refusal outcomes tracked.

---

## When the owner reports an error

This will happen, and it is a normal part of the loop, not a failure of
it. When the owner tests the system and something is wrong:

1. **Reproduce it first.** From the diagnostic bundle, the logs, or the
   description. A fix for an error you have not reproduced is a guess.
2. **The bug becomes a failing test before it becomes a fix** — that is
   how the same error is prevented from returning.
3. **Route it to the owning agent** from the ownership table. A cost bug
   goes through `cost-auditor`'s checklist; a sizing bug goes to the risk
   area and gets `risk-reviewer`'s read before merging; a dashboard bug
   gets rendered and measured, not eyeballed.
4. **Fix, run the full suite, and confirm the original report** — state
   plainly what was wrong, what changed, and how it was verified. "Should
   be fixed now" is not a report.
5. If the error revealed a class of problem rather than an instance, say
   so, and have `stress-tester` attack the class.

The owner reports symptoms, not diagnoses. "The dashboard shows $NaN" is
a complete and sufficient bug report — never ask them to debug.

## Rules

1. **Verify by running it.** Almost every failure in the previous build was
   an empirical claim asserted rather than tested. Another agent reviewing
   your reasoning shares your priors and will agree with you.
2. **Never report a fix you have not confirmed landed.**
3. **Every zero needs its raw response printed beside it.**
4. **A test that cannot fail is not a test.** Break a copy and confirm the
   check catches it.
5. **Keep the test suite fully offline.** No network calls from tests.
6. **Version control from the first commit.**
7. **Never commit credentials. Never place a live order without explicit
   sign-off.**
8. Prefer boring, inspectable code. This handles money unattended.

---

## Reference implementation

A previous working implementation is available: a functioning broker
layer, four live data feeds, ~458 offline tests, and the observability
described above. Treat it as evidence of what works, not as a starting
point. You may ignore it entirely — but every trap listed above came from
it, and every one is real.
