# Handover — what was built, what it can do, and what to believe

Written 2026-08-10 at the end of the eight-stage build. This document is
deliberately blunt: the numbers you can trust are labeled, the ones you
cannot are labeled louder.

---

## What exists

A complete, tested, installable autonomous trading system:

- **Backtest harness** with point-in-time discipline (look-ahead is a
  raised error, not a convention), SPY benchmark on every run, a
  random-strategy null test, and survivorship caveats embedded in every
  persisted result.
- **Cost tracking and governor** accurate to the cent: record-first
  ledger, cache-token and web-search pricing, whole-day reconciliation
  against the Anthropic Cost API, and a hard $5/month scheduled cap that
  only realized **live** profit can raise (never above $8, never by the
  system's own hand past that clamp).
- **The trading pipeline** for the winning strategy (insider cluster
  buying): live EDGAR Form 4 feed → adapter → discovery (identical
  cluster definition to the graded backtest, enforced by a parity test)
  → Claude research boundary (the only code path that can spend on a
  model call; forced-schema output; strict validation) → deterministic
  risk engine (sizing can only see a pass/fail bool from the model) →
  broker execution with cancel-confirm stop discipline → settlement.
- **Kill switches** that fail closed, protective duties that run even
  when a loss-rule switch has tripped, and a refusal tracker that scores
  every declined candidate after its counterfactual window.
- **Adaptive parameters** that move only on deduplicated, provenance-
  checked closed outcomes, with minimum samples, 3:1 tighten:loosen
  asymmetry, bounded steps, disjoint evidence windows and auto-revert.
- **Evidence graph** (informational only — an AST-level test forbids it
  from importing risk or execution code).
- **Dashboard** on port 8000: performance vs SPY net of costs as the top
  element, the candidate funnel with the blaming stage named, per-trade
  decision traces, refusal outcomes, searchable logs, redacted
  diagnostics. Every zero prints its raw upstream response.
- **One-command install** (`sudo bash install/install.sh`), upgrade with
  automatic rollback on any test failure, and a browser setup page for
  credentials — including an explicit, loudly-labeled paper/live
  selector. Nobody ever edits a config file.

State of verification: **~660 offline tests** (sockets blocked,
credentials stripped), every mechanism sabotage-verified (break a copy,
prove the test catches it — see `tests/SABOTAGE-LOG.md`), three
adversarial risk reviews and three stress passes with every blocking
finding fixed, and live paper-account verification of all three order
legs (place→reconcile→cancel, rejection recording, fill→sell→flat).

## The strategy, and the honest number

**Candidate C: insider cluster buying.** Two or more distinct insiders
buying ≥$50k combined in the open market within 10 calendar days
(Form 4, code P, 10b5-1 plan trades excluded), entered at the next open
after the *filing* date, held 12 days, equal-weight slots, hard stops.

The bake-off's conclusion, quoted rather than improved:
**nothing beats SPY out-of-sample net of all costs, robustly.**

| The number | Value | Believe it? |
|---|---|---|
| C's OOS excess vs SPY, net, 2024→2026 | **+6.73pp** (+75.45% vs +68.72%) | The measurement is honest; the margin is thin |
| OOS sample | 229 trades, hit 52.8%, mean +1.75%/trade | Yes, as a sample |
| Population mean per trade | +0.73% | The +1.75% subsample beat this — some of the OOS result is luck |
| Full-period (2016→2026) excess | **−418pp** (C loses badly over the whole decade) | Yes — the edge, if real, is regime-dependent |
| Measured spread cost | median 16.4bp round trip; worst decile breaches the kill line | Yes — hence the hard 20bp half-spread entry gate |
| At $0/month API cost | +6.73pp becomes +31.64pp | The API bill is most of the hurdle at this account size |

**Plain reading:** this strategy earned its place by being the only arm
with a positive OOS excess under pre-registered rules, not by being
convincingly good. A +6.73pp margin over 2.5 years, where the same rules
lost −418pp over the full decade, is a hypothesis worth paper-trading,
not a proven edge. The refusal tracker exists to turn that hypothesis
into a number.

## Evidence graph: design and runtime cost

Two tables (`graph_entities`, `graph_assertions`) with CHECK-enforced
provenance — a model inference can never claim to be a primary document.
Research findings batch into the graph in one transaction with **zero
model passes**; rendering context for a prompt is a bounded, cycle-safe
SQL traversal. Runtime cost: pennies of storage, no API spend. Its
context adds ~a few hundred tokens per research prompt (≈$0.001/call at
Sonnet rates) only when the graph actually knows something.

## Data sources verified live (see docs/DATA-SOURCES.md for all 25)

The load-bearing ones: EDGAR daily index + full submissions (the Form 4
feed; EFTS cannot enumerate — measured), SEC insider transaction data
sets (the backtest's history), data.sec.gov XBRL companyfacts
(first-filed values only), Alpaca account/market data (paper account
facts confirmed live, including the corrected `/v1/corporate-actions`
endpoint), ClinicalTrials.gov v2, Federal Register, openFDA
(retrospective only — cannot predict decisions).

## Specialist findings that mattered (all fixed, all in the PR trail)

1. The orders table's foreign key pointed at the wrong table — every
   live entry would have crashed *after* the buy reached Alpaca,
   invisible offline because tests ran SQLite with FKs off. (stress)
2. No position-close path existed; hard exits would have bricked the
   cycle on the first exit date, forever. (risk review round 1)
3. A tripped kill switch abandoned open positions entirely — the exact
   opposite of protection. (round 1)
4. Stops were placed before entries filled; the cash account rejected
   them silently, leaving positions unprotected. (round 1)
5. A single transient 404 could void a real position. (round 3)
6. Coercing log arguments to strings in the redaction filter broke
   number formatting process-wide. (merge dry-run)
7. Paper fills credit sale proceeds to *both* cash fields instantly —
   **Alpaca paper does not simulate T+1 settlement**, so the settled-
   cash clamp is unverifiable until live. (live verification)

## Known gap: two machines, one Alpaca account

The single-instance lock (`orchestrator/instance_lock.py`) prevents two
Catalysts on **one machine**. It cannot see a second install on a
different server pointed at the same Alpaca account, and the existing
broker reconciliation would not catch the likeliest form of it: both
machines run the same discovery on the same data, so both buy the
**same** ticker, and `_broker_positions_agree` compares symbol *sets*,
never quantities. Each machine then rests its own DAY stop over the
same shares; whichever triggers second oversells.

Two cheap fixes were identified by the risk review and deliberately
NOT taken here, because both touch execution/broker code and belong in
their own reviewed change (house rule 5):

1. compare **quantities** in `_broker_positions_agree`, tolerating
   pending fills — this also catches the owner trading manually in the
   same account;
2. persist a per-install instance id, prefix every `client_order_id`
   with it, and block on any open broker order carrying an unfamiliar
   prefix.

Until then the rule is operational: **run exactly one Catalyst against
one Alpaca account.**

## Least-confident areas — read before trusting money to this

1. **The edge itself.** See above. Paper-trade until the refusal tracker
   and closed trades give a sample; the dashboard says how far that is.
2. **Settled cash on a live account** (finding 7). Before going live,
   verify which Alpaca field reflects T+1, or add local settlement
   tracking from our own fills. The current `min(cash, nmbp)` read is
   conservative but unproven against a live account.
3. **Worst case per position is NOT 2%.** The 2% figure is a sizing
   input. A max position is 20% of equity; a 50% overnight gap costs
   ~10% of the account; DAY stops do not exist overnight. This is
   inherent to overnight holding of small caps, and it is why positions
   are small and clustered exposure is capped — but it needs eyes-open
   acceptance.
4. **Ticker trust.** A Form 4 claiming ticker SPY would become a
   candidate; nothing cross-checks issuer CIK against the symbol, and no
   universe rule excludes funds (deliberate deferral — a strategy
   decision; an xfail test carries the desired behavior).
5. **Three residual xfail deferrals** in the suite mark accepted,
   documented gaps (orders-layer duplicate-stop reduction, the
   never-filled ordered-qty stop fallback that self-heals within one
   session, and the ticker rule above).
6. **Adaptive starting values** (conviction floor 0.60, adverse gap
   0.08, stop width 0.10) are estimates. Only the 12-day hold is
   measured. They will move slowly, on evidence, and the dashboard shows
   how far each is from having enough.
7. **The research boundary is now live-verified** (2026-08-10, real
   keys, manual spend, 32.6¢ total). A real discovered candidate (GBFH
   insider cluster) went through `investigate()` end to end: web search
   ran, the forced extraction returned a complete view, and the view was
   sharp — it identified that "three insiders" were one CEO plus two
   LLCs he manages buying identical amounts, and returned `no_trade` at
   0.15 conviction. Three real API drifts surfaced and were each caught
   by the record-first discipline before any spend was mispriced:
   `output_tokens_details` (a breakdown of output_tokens, not extra
   billing), `server_tool_use.web_fetch_requests` (informational, web
   fetch is not metered), and the live API not enforcing `required` on
   forced tool calls (fixed with one bounded repair turn). Actual cost
   of a full researched candidate with 2 web searches: **18.7¢** —
   above the 13¢ estimate below, still inside the cap at the expected
   cadence.
8. **The Cost API adapter is live-verified with a real admin key.**
   Amounts confirmed as decimal strings in CENTS (priced 2026-08-04's
   real token volumes against the reported amount — 100× apart from the
   dollars reading). Reconciliation runs nightly for closed days only;
   the adapter is read-only by construction and a test forbids any
   mutating HTTP verb in the module.

## Next steps, in order

1. Install on the VPS (`sudo bash install/install.sh`), enter Alpaca
   paper keys + an Anthropic key in the setup page, leave the selector
   on **paper**.
2. Let it run. Watch the funnel: it should find a few insider-cluster
   candidates a week and research the best of them within the $5/month
   cap. Refusals are normal and are being scored.
3. After a meaningful closed sample (the dashboard's small-sample
   banners disappear as thresholds are met), read the performance panel
   against SPY, *net of the API bill*.
4. Only then consider the live switch — after resolving least-confident
   items 2 and 3 above. Live is a deliberate choice in the setup UI, and
   the governor only counts live profit toward its cap.

## Cost expectation

Scheduled spend is governed to $5/month base (≈6%/year hurdle on
$1,000). The first live researched candidate cost a **measured 18.7¢
at standard rates, 13.2¢ at the Sonnet 5 intro rates Anthropic is
actually billing through 2026-08-31** (24k input tokens, 2.3k output,
two web searches at 1¢ each). The pricing table is date-effective
(pricing.py: intro window found empirically against the owner's
console figure) and the first closed-day reconciliation is the hard
check on it.
At that measured cost, ~26 researched candidates/month fit the cap;
the realistic cadence (a few a week) lands well under it. The governor's
pre-call estimates were raised to the measured reality plus margin
(15¢ exploration + 8¢ extraction — the original 8¢+5¢ "pessimistic"
figures were 44% under what the first live call actually cost). The
dashboard separates scheduled from manual spend and states the annual
hurdle.
