# Catalyst trading bot

An autonomous bot that trades US equities unattended. **Claude finds the
opportunities, researches them and decides whether they are worth taking.
Deterministic code decides how much money is at stake.**

Full spec: @docs/BUILD-BRIEF.md — the original brief, kept as written.
Where this file and the brief disagree, **this file is what the code
does**; the brief is the goal it was built toward.
Facts that cost real money to learn: @docs/TRAPS.md — read before
writing any cost tracking, data feed, or broker code.

---

## What the bot actually does, as of 2026-08-17

Every 15 minutes, unattended:

1. **Feeds collect evidence.** EDGAR Form 4, EDGAR full-text search,
   Federal Register, ClinicalTrials.gov, Alpaca news. Free, keyless
   where possible, cached. This costs nothing per item.
2. **Two things turn evidence into candidates.**
   - The **mechanical screen** — Form 4 insider clusters and cross-feed
     conjunctions. This is line-for-line the arm that was backtested, so
     its measured edge means something.
   - **Claude's hunt** — once a day (more if the budget allows), Claude
     reads the raw feed and nominates what the screen has no rule for.
     It may only cite events that already exist; every nomination is
     validated against them.
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
| $100 | $10.00 | 6 | 1 |
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
- **Claude learns nothing between calls.** No past outcome reaches the
  research prompt. It judges every candidate as if it were the first.
  Closing that loop is the highest-value next change, and it needs
  closed trades before there is anything to feed back.
- **The conviction scale is newly defined and uncalibrated.** Whether a
  0.6 call really resolves six in ten is unknown.

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
