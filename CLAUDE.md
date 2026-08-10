# Catalyst trading bot

An autonomous bot that trades US equities unattended, using Claude to
make the judgements and deterministic code to make the decisions.

Full spec: @docs/BUILD-BRIEF.md — read before designing anything.
Facts that cost real money to learn: @docs/TRAPS.md — read before
writing any cost tracking, data feed, or broker code.

## The strategy is not decided

**Finding the best way to make money is the work.** No strategy is
prescribed. The brief describes one previous attempt and what happened;
that is evidence, not direction. Propose several approaches, grade them
on the backtest, keep what wins.

## The one rule that is not negotiable

**The model proposes, deterministic code disposes.** Claude returns a
view on a candidate. Code decides whether to trade, how large, and when
to exit. The model never sizes a position or places an order.

## Fixed constraints

- Trading capital: **$1,000**, paper account until proven.
- **No Pattern Day Trader rule.** It was retired 4 June 2026. Unlimited
  day trades on any account size. But margin still needs $2,000, so
  design for **no leverage**.
- Hold **days to weeks, never months.** Hard exit date on every position.
- **Three to five positions**, genuinely uncorrelated. Four biotech
  binaries resolving the same fortnight is one bet, not four.
- **A few trades a month.** A sample you never accumulate teaches nothing.
- Runtime API budget: **£20/month is the ceiling, not the target.** Aim
  for £7-10. At £20 the bot must beat ~30%/year just to match cash.
  Design to be cheap; every model pass in the live path is charged
  forever.

## Runtime

Ubuntu VPS, systemd, unattended. Dashboard on port 8000, bound to
0.0.0.0. The VPS is IP restricted. Credentials never in the repo.

## Thresholds are measured, not asserted

Conviction floors, gap assumptions and stop widths start as estimates and
must adapt on **closed, scored outcomes** — never on projections or the
model's own confidence. The refusal tracker is the main feedback loop:
score what declined candidates went on to do.

But **hard bounds never move by themselves** — max loss per position,
total exposure, kill switches. Those prevent ruin; the system may propose
changes, a human decides. Tighten fast on evidence of harm, loosen slowly
on evidence of over-caution, and log every adjustment with the evidence
behind it.

## Not optional

- **One-command install, and a UI for entering credentials.** Nobody is
  ever told to edit a config file.
- **Every trade must be explainable after the fact** — what the model
  saw, what it concluded, what the risk engine did, what happened.
- **Logs searchable from the dashboard.** No SSH required to troubleshoot.

## Avoiding collisions

Agree interfaces before parallel work. One owner per file — see the
ownership table in the brief. Branch per task, merge one at a time,
run the tests between merges. Schema and config changes go through a
single session, never two at once.

## Finished work goes to `main`, or it does not exist

**The VPS follows `main`.** `install/upgrade.sh` runs `git pull
--ff-only` on whatever branch the machine is on, so work sitting on a
feature branch is invisible to the owner no matter how well it is
tested. It is not shipped until `main` has it.

This has already cost an evening: six commits of finished, green work
sat on a branch while the owner ran the upgrade, was told "Upgrade
complete", saw the same version before and after, and went hunting
through browser caches for a change that had never reached the machine.

So, whenever you say a change is ready for the owner to upgrade:

1. **Land it on `main` first**, then tell them. Never announce work as
   ready to upgrade while it is only on a branch.
2. **Say which commit is on `main`** when you report it, so "did it
   ship" is a check rather than a memory.
3. **Confirm, do not assume** — `git log --oneline origin/main -1` and
   `git log origin/main..HEAD` (the second must be empty). Rule 2 below
   applies: never report a landing you have not confirmed.
4. The exception is **risk, execution or broker code**, which needs
   human review first (house rule 5). Open a PR, say plainly that it is
   waiting on them, and do **not** describe it as ready to upgrade
   until it is merged.

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
4. A test that cannot fail is not a test — break a copy, confirm it catches it.
5. Changes to risk, execution or broker code need human review.
