# Catalyst — System Architecture

Status: **proposed, unbuilt**. This document is the single agreed interface
contract the brief requires before any parallel work starts (BUILD-BRIEF.md
§ "How the agents avoid conflicting" → "Agree the interfaces before anything
is built"). Nothing in `catalyst/` exists yet; this defines what gets built
and by whom. Signatures below are interface contracts, not implementations —
bodies are elided with `...`.

For every major decision this document states the alternative that was
rejected and why, per the task's own request for reasoning over conclusions.

---

## 1. Design principles this architecture serves

Five constraints from CLAUDE.md and the brief drove every structural choice
below; naming them up front makes the "why" sections legible without
re-deriving them each time.

1. **The model proposes, code disposes** — structurally, not just by
   convention (§4).
2. **Every trade must be reconstructable after the fact** — the data model
   is event-sourced for this reason, not for general good taste (§5).
3. **Hard bounds never move themselves; adaptive parameters move slowly on
   evidence** — two physically separate stores with different write paths
   (§6).
4. **The cost governor gates spend before it happens**, using a locally
   computed running total, because the Cost API cannot tell you today's
   spend (§7, TRAPS.md).
5. **One owner per file; shared files go through a single session** — the
   module boundaries below are drawn so that "shared file" is the
   exception, not the default (§8).

---

## 2. Module structure

```
catalyst/
  data/                 data-engineer
    sources/              one adapter file per source (federal_register.py,
                           edgar.py, clinicaltrials.py, alpaca_news.py,
                           openfda.py, ...)
    normalize.py           raw source payloads -> RawEvent
  discovery/             strategy-analyst
    candidates.py          RawEvent[] -> Candidate[] (strategy-specific
                            definition of "tradeable event")
    correlation.py          sector / catalyst-type / resolution-date
                            clustering used by risk.evaluate
  research/
    prompts.py             strategy-analyst — question design, tool
                            definitions offered to Claude
    schema.py               human-review-required — the ResearchView
                            contract; structurally cannot carry a size
    boundary.py             human-review-required — calls Claude, enforces
                            the tool-forced schema, records the call,
                            never lets raw text reach a numeric path
  risk/                  human-review-required
    hard_bounds.py          frozen config; only a human-authored commit
                            changes it; no importer outside this file may
                            write to it
    adaptive_params.py      the adaptive-parameter store: evidence intake,
                            bound checks, step limits, logging, revert
    sizing.py                deterministic position sizing
    evaluate.py              risk.evaluate() — the single gate every
                            candidate passes through
    kill_switches.py         daily-loss / drawdown / consecutive-loss
                            checks, run every cycle before anything else
  execution/             human-review-required
    broker.py               thin Alpaca adapter
    orders.py                order construction, stop placement
    reconcile.py              fill reconciliation against broker state
    exits.py                  time-based and stop-triggered exits
  cost/                  cost-auditor
    tracker.py               raw usage capture, cent-accurate pricing
    governor.py               pre-call authorization against the budget
    ledger.py                 scheduled vs. manual spend, monthly rollup
  backtest/              backtest-engineer
    harness.py                replay engine, point-in-time data access
    structural.py              free, no-Claude signal backtests
    judgement.py               optional, costed, calls research.investigate
    scoring.py                 sample size, hit rate, drawdown, in/out-of-sample
  storage/               SHARED — single session, no parallel edits
    schema.sql               table definitions (§5)
    migrations/
  dashboard/             ui-designer
    api/                     read endpoints over storage
    static/                  served UI
  orchestrator/          integration-engineer
    cycle.py                 wires discovery -> research -> risk -> execution
    scheduler.py              systemd-invoked entry point
  install/               integration-engineer
    install.sh, upgrade.sh, setup_ui/
  tests/                 test-writer + stress-tester, shared by file
```

Every arrow below (`discovery -> research -> risk -> execution`) is a
function call through the signatures in §3, not a message queue or an
internal API — this is one process, one systemd service, per the brief's
"Ubuntu VPS, systemd, unattended."

### 2.1 Module responsibilities

| Module | Responsible for | Not responsible for |
|---|---|---|
| `data/` | Fetching, normalizing, rate-limiting, retry-on-5xx per source. Returns `RawEvent`, source-agnostic. | Deciding whether an event is tradeable. |
| `discovery/` | Turning `RawEvent[]` into `Candidate[]` — the strategy-specific definition of "this is a dated, tradeable event." Correlation/concentration clustering. | Fetching data. Judging the candidate. |
| `research/` | Asking Claude about a `Candidate`, enforcing the structured-output contract, recording the full call. | Deciding whether to trade or how large. |
| `risk/` | The only place a number becomes a position size, a stop, or a kill decision. Owns both bound stores. | Researching a candidate, placing an order. |
| `execution/` | Talking to Alpaca: orders, stops, reconciliation, exits. | Sizing. |
| `cost/` | Whether a call is authorized, what it cost, the running ledger. | Everything else costs money too (API infra, VPS) — out of scope for this module, in scope for `install/`'s documented runtime budget. |
| `backtest/` | Replaying history against a pluggable strategy signal or the real research/risk code. | Live trading. |
| `storage/` | Schema only. No business logic. | Anything else — this file is intentionally thin so it stays a safe shared surface. |
| `dashboard/` | Read-only rendering of `storage/` state plus the setup-credentials write path. | Writing trade state. |
| `orchestrator/` | Sequencing one cycle: call discovery, then research, then risk, then execution, in that order, with the cost governor and kill switches checked first. | Any domain logic — it should be thin enough that reading it *is* reading the pipeline order. |

---

## 3. Interfaces between modules

Data shapes first (§3.1), then the function signatures each module exposes
to its neighbors (§3.2). These are frozen once this document merges — a
change to any of them is a schema/config-class change and goes through the
single coordinating session per §8.

### 3.1 Core data shapes

```python
# --- data/ -> discovery/ ---

@dataclass(frozen=True)
class RawEvent:
    source: str                    # "federal_register", "edgar", ...
    source_id: str                 # source's own identifier, for de-dup
    fetched_at: datetime
    payload_raw: dict              # verbatim upstream JSON, never trimmed
    # No interpretation happens here. A RawEvent is evidence, not a claim.


# --- discovery/ -> research/ ---

@dataclass(frozen=True)
class Candidate:
    id: str                        # ULID, assigned at discovery time
    ticker: str
    catalyst_type: str              # enum: "fda_decision", "merger_vote",
                                     # "clinical_readout", "earnings", ...
    catalyst_date: date              # best estimate of resolution date
    catalyst_date_confidence: str    # "confirmed" | "estimated"
    source_event_ids: list[str]      # RawEvent.source_id chain, for audit
    discovered_at: datetime
    sector: str
    correlation_tags: list[str]      # for concentration checks in risk/


# --- research/ -> risk/  (the boundary object — see §4) ---

@dataclass(frozen=True)
class ResearchView:
    candidate_id: str
    direction: Literal["long", "short", "no_trade"]
    conviction: float               # 0.0-1.0, model's stated confidence
    thesis: str                     # free text, for the audit trail only
    invalidation: str               # what would prove this wrong
    expected_holding_days: int
    priced_in: bool
    priced_in_reasoning: str
    # Deliberately absent: any field shaped like a size, a share count,
    # a dollar amount, or an order type. See §4.1.


@dataclass(frozen=True)
class ResearchCallLog:
    id: str
    candidate_id: str
    model: str
    prompt_rendered: str             # exact prompt sent, for replay/audit
    tools_offered: list[str]
    tool_calls: list[dict]           # each call + its raw result
    raw_response: dict               # entire API response, verbatim
    usage_raw: dict                  # verbatim usage object — see TRAPS.md
    parsed_view: ResearchView | None # None if the call was skipped/failed
    cost_cents: Decimal
    latency_ms: int
    skipped_reason: str | None       # e.g. "budget_denied", "api_error"


# --- risk/ -> execution/ ---

@dataclass(frozen=True)
class RiskDecision:
    candidate_id: str
    action: Literal["trade", "skip"]
    side: Literal["long", "short"] | None
    notional_usd: Decimal | None
    qty: Decimal | None              # fractional shares allowed
    stop_price: Decimal | None
    planned_exit_date: date | None
    limits_applied: list[LimitApplication]
    skip_reasons: list[str]          # populated when action == "skip"
    adaptive_params_snapshot: dict   # values in effect at decision time


@dataclass(frozen=True)
class LimitApplication:
    rule_name: str                   # e.g. "max_loss_per_position"
    bound_value: Decimal
    requested_value: Decimal
    bound_type: Literal["hard", "adaptive"]
    binding: bool                    # did this rule actually constrain the trade?


# --- execution/ -> storage/ ---

@dataclass(frozen=True)
class OrderResult:
    decision_id: str
    broker_order_id: str
    status: str
    submitted_at: datetime

@dataclass(frozen=True)
class Fill:
    order_id: str
    price: Decimal
    qty: Decimal
    filled_at: datetime
    broker_reported_price: Decimal   # kept distinct from any modeled price
```

### 3.2 Function signatures

```python
# data/sources/<source>.py — one such module per source, same shape
def fetch_events(since: datetime, until: datetime) -> list[RawEvent]: ...
    # Fails soft: a dead feed returns [] and logs why (source, error,
    # timestamp) to storage.raw_events_errors. Never raises into the caller.

# discovery/candidates.py
def build_candidates(
    raw_events: list[RawEvent],
    as_of: datetime,               # for backtest point-in-time discipline
) -> list[Candidate]: ...

# discovery/correlation.py
def cluster(candidates: list[Candidate], open_positions: list[Position]) -> dict[str, list[str]]: ...
    # Returns candidate_id -> cluster_key, where cluster_key encodes
    # sector + catalyst_type + resolution-week. risk.evaluate() uses this,
    # not raw ticker count, to judge concentration.

# research/boundary.py
def investigate(
    candidate: Candidate,
    cost_context: CostContext,      # from cost/governor.py — see §7
) -> ResearchCallLog: ...
    # Internally: cost.governor.authorize() first; if denied, returns a
    # ResearchCallLog with skipped_reason set and parsed_view=None — never
    # silently does nothing. Uses a forced tool_choice against the
    # ResearchView JSON schema (see §4.2); no other code path produces a
    # ResearchView.

# risk/evaluate.py
def evaluate(
    candidate: Candidate,
    view: ResearchView,
    portfolio: PortfolioState,
    params: AdaptiveParamSnapshot,   # read-only snapshot, see §6
) -> RiskDecision: ...
    # The only function in the system permitted to construct a
    # notional_usd or qty value. See §4 for the enforcement argument.

# risk/kill_switches.py
def check(portfolio: PortfolioState, hard_bounds: HardBounds) -> KillSwitchState: ...
    # Called once per cycle, before any candidate is evaluated. If tripped,
    # the orchestrator skips straight to execution.manage_exits() and
    # blocks new entries for the rest of the cycle.

# risk/adaptive_params.py
def propose_adjustment(parameter: str, evidence: EvidenceSample) -> AdjustmentProposal: ...
def apply(proposal: AdjustmentProposal, hard_bounds: HardBounds) -> AdaptiveParamLogEntry: ...
    # apply() re-checks the proposal against hard_bounds itself — it does
    # not trust the caller to have checked. See §6.3.

# execution/orders.py
def place(decision: RiskDecision) -> OrderResult: ...
# execution/reconcile.py
def reconcile() -> list[Fill]: ...
# execution/exits.py
def manage_exits(portfolio: PortfolioState, as_of: datetime) -> list[ExitAction]: ...

# cost/governor.py
def authorize(estimate: CostEstimate, kind: Literal["scheduled", "manual"]) -> GovernorDecision: ...
# cost/tracker.py
def record(usage_raw: dict, kind: Literal["scheduled", "manual"], component: str) -> CostEvent: ...

# backtest/harness.py
def replay(
    signal_fn: Callable[[Candidate, PointInTimeData], ResearchView],
    universe: list[Candidate],
    date_range: tuple[date, date],
) -> BacktestResult: ...
    # signal_fn is pluggable: a deterministic strategy function for the
    # free structural backtest, or research.investigate wrapped for the
    # costed judgement backtest. See §9 decision on this split.
```

---

## 4. Where "Claude decides" ends and "code decides" begins

### 4.1 The boundary is structural, not procedural

`ResearchView` (§3.1) has no field that can hold a position size, a share
count, a dollar amount, or an order type. This is deliberate and is the
primary enforcement mechanism: **there is no path from a `ResearchView`
instance to a sizing calculation that doesn't first go through
`risk/sizing.py`, because the object physically cannot carry a size.**

A second, independent enforcement layer sits in the API call itself:
`research/boundary.py` uses a forced `tool_choice` against a JSON schema
matching `ResearchView` exactly (see §4.2). Claude's response is only ever
consumed through the parsed, schema-validated object — free-form prose the
model writes is stored in `ResearchCallLog.raw_response` for the audit
trail (§5) but no code path reads it for anything numeric. If Claude's
prose happens to contain something that looks like a size ("a $200
position seems reasonable"), it never reaches an arithmetic operation,
because nothing downstream of `investigate()` reads `raw_response` except
the dashboard's narrative view.

### 4.2 Enforcing the schema at the API layer

`research/boundary.py` calls Claude with `tool_choice` forced to a single
tool, `submit_research_view`, whose `input_schema` is generated from
`ResearchView`'s field set (`direction`, `conviction`, `thesis`,
`invalidation`, `expected_holding_days`, `priced_in`,
`priced_in_reasoning` — nothing else). This is standard Anthropic
tool-use: forcing `tool_choice` to one tool means the API's only valid
completion is a call to that tool, so `investigate()` can assert
`response.stop_reason == "tool_use"` and parse `tool_use.input` directly
into `ResearchView` without ever touching a free-text block for the
canonical result.

### 4.3 Conviction gates; it does not size

`risk/evaluate.py` uses `view.conviction` exactly once: compared against
`adaptive_params.conviction_floor` to produce a boolean trade/no-trade
gate. It is never multiplied into a size, never bucketed into a size
tier, and never otherwise touches `sizing.py`'s arithmetic. Approved
trades of the same risk category are sized identically regardless of
whether conviction was 0.66 or 0.99 above the floor. See §9.8 for why this
stricter reading was chosen over a conviction-weighted or bucketed
alternative.

### 4.4 What "code decides" covers, concretely

Everything downstream of the gate is `risk/sizing.py` reading only:
account equity, `adaptive_params_snapshot` (stop widths, adverse-gap
assumptions by catalyst type), `hard_bounds` (max loss per position, max
exposure), and market data (volatility, spread) fetched independently of
anything Claude said. `RiskDecision.limits_applied` records every rule
that touched the outcome and whether it bound, so the dashboard can show
"the model rated this 0.81 conviction; code capped it at $180 because of
the sector-concentration limit" as a factual reconstruction, not a
narrative.

---

## 5. Data model — reconstructing any trade decision

Storage is **event-sourced and append-only** for anything decision-related
(§9.1 explains the rejected alternative). Every table below is linked by
`candidate_id` so the dashboard's "why did this trade happen" view (a
BUILD-BRIEF.md requirement) is a straightforward join chain, not a
best-effort reconstruction from mutable rows.

```sql
-- data/ layer
raw_events            (source, source_id, fetched_at, payload_raw)
raw_events_errors     (source, attempted_at, error_text)  -- fail-soft record

-- discovery/ layer
candidates            (id, ticker, catalyst_type, catalyst_date,
                        catalyst_date_confidence, source_event_ids,
                        discovered_at, sector, correlation_tags)

-- research/ layer
research_calls        (id, candidate_id, model, prompt_rendered,
                        tools_offered, tool_calls, raw_response,
                        usage_raw, cost_cents, latency_ms,
                        skipped_reason, called_at)
research_views        (candidate_id, direction, conviction, thesis,
                        invalidation, expected_holding_days, priced_in,
                        priced_in_reasoning)

-- risk/ layer
risk_decisions         (candidate_id, action, side, notional_usd, qty,
                        stop_price, planned_exit_date, skip_reasons,
                        adaptive_params_snapshot, decided_at)
limit_applications     (decision_id, rule_name, bound_value,
                        requested_value, bound_type, binding)
refusals               (decision_id, candidate_id, price_at_refusal,
                        refused_at, scored_at, outcome_price,
                        outcome_return)  -- scored_at/outcome_* filled by
                                          -- an async job days/weeks later
kill_switch_events     (triggered_at, switch_name, portfolio_state_snapshot,
                        cleared_at)
adaptive_param_log     (parameter, old_value, new_value, sample_ids,
                        evidence_summary, changed_at, reverses_to,
                        reverted_at)

-- execution/ layer
orders                (id, decision_id, broker_order_id, side, qty,
                        order_type, time_in_force, submitted_at, status)
fills                 (order_id, price, qty, filled_at,
                        broker_reported_price, modeled_slippage)
positions             (id, ticker, entry_order_ids, stop_order_id,
                        opened_at, planned_exit_date, status)
closed_trades          (position_id, entry_price, exit_price, exit_reason,
                        realized_pnl_cents, expected_holding_days,
                        actual_holding_days, closed_at)

-- cost/ layer
cost_events            (raw_usage_json, kind, component, priced_cents,
                        priced_at, api_call_id)
cost_governor_events   (cycle_id, requested_kind, estimate_cents,
                        cap_cents, decision, reason, at)
```

Every "empty" query result the dashboard shows carries the raw upstream
response beside it (a `raw_events_errors` row, or a `research_calls` row
with `skipped_reason` set) — this satisfies CLAUDE.md house rule 3 ("every
zero gets its raw upstream response printed beside it") at the schema
level rather than relying on the UI layer to remember to fetch it.

### 5.1 Reconstructing one trade end-to-end

For any `closed_trades.position_id`: join backward through
`orders -> risk_decisions -> research_views -> research_calls ->
candidates -> raw_events` (via `source_event_ids`) and forward through
`fills`. Every field the brief's "Show why every trade happened" section
requires — what the model saw, what it concluded, what risk did, what
happened — is one query away, not reconstructed from logs.

---

## 6. Adaptive thresholds

### 6.1 Two stores, physically separate

```python
# risk/hard_bounds.py  — human-authored, human-reviewed, no runtime writer
@dataclass(frozen=True)
class HardBounds:
    max_loss_per_position_pct: Decimal      # e.g. 0.10 (of account)
    max_total_exposure_pct: Decimal
    max_open_positions: int
    daily_loss_kill_pct: Decimal
    drawdown_kill_pct: Decimal
    max_correlated_cluster_pct: Decimal      # concentration limit

HARD_BOUNDS = HardBounds(...)   # literal values, committed to this file only
```

`risk/hard_bounds.py` exports `HARD_BOUNDS` as a module-level frozen
constant, loaded from a YAML file that is **not** on any write path from
`adaptive_params.py` — no function in `risk/adaptive_params.py` imports or
writes this module. Changing a hard bound requires a human-authored PR
that a human merges; the system's only lever is
`adaptive_params.propose_adjustment()` returning a proposal *for a human
to read* when it detects a hard bound looks miscalibrated (e.g., it binds
on every trade of a catalyst type) — it cannot apply that proposal itself.

```python
# risk/adaptive_params.py — the only writer of adaptive state
ADAPTIVE_PARAMETERS = [
    "conviction_floor",
    "adverse_gap_assumption",        # per catalyst_type
    "stop_width",                    # per catalyst_type
    "holding_period_estimate",       # per catalyst_type
    "search_budget_allocation",      # per catalyst_type
]

MIN_SAMPLE_SIZE = {
    "conviction_floor": 30,          # closed, scored trades
    "adverse_gap_assumption": 20,    # per catalyst type
    "stop_width": 20,
    "holding_period_estimate": 15,
    "search_budget_allocation": 40,
}
```

`MIN_SAMPLE_SIZE` values are a starting estimate, not a claim of statistical
rigor — the brief requires the minimum be stated and defended (BUILD-BRIEF
§ "The rules adaptation must follow", rule 2), so these are logged as
proposals subject to revision once `backtest/` produces power-analysis
numbers against real catalyst-type variance. Until then they're a
placeholder floor, not a validated threshold — and the dashboard must say
so (§9.9 covers why this can't be hidden behind a confident-looking
number).

### 6.2 Belt-and-suspenders enforcement

Three independent layers, so that a bug in one doesn't silently permit a
hard-bound breach:

1. **Structural**: `adaptive_params.py` has no import of, or reference to,
   `hard_bounds.HARD_BOUNDS` as a writable target. `hard_bounds.py`
   exposes only a frozen dataclass — there is no setter to call.
2. **Process**: the file `risk/hard_bounds.py` requires `risk-reviewer`'s
   sign-off on every PR that touches it (see §8 ownership table) and is
   explicitly called out in CLAUDE.md house rule 5 ("Changes to risk,
   execution or broker code need human review").
3. **Runtime guard**: `adaptive_params.apply()` takes `hard_bounds` as an
   explicit read-only argument and, before writing `adaptive_param_log`,
   checks that the *worst-case downstream effect* of the new value — run
   through `risk/sizing.py`'s own worst-case calculation — does not exceed
   any `HardBounds` field. If it would, `apply()` refuses and logs why,
   with the shortfall amount, matching the brief's requirement that "the
   dashboard says which bound stopped it and by how much."

### 6.3 The adaptation rules, encoded

- **Closed, scored outcomes only**: `EvidenceSample` (the input to
  `propose_adjustment`) is only ever constructed from `closed_trades` rows
  (`status = 'closed'`) or scored `refusals` rows (`scored_at IS NOT
  NULL`). There is no constructor path from unrealized P&L or a
  `ResearchView.conviction` value.
- **Minimum sample**: `propose_adjustment` refuses (returns
  `AdjustmentProposal(applicable=False, reason="insufficient_sample")`)
  below `MIN_SAMPLE_SIZE[parameter]`.
- **Asymmetric speed**: `apply()`'s step-size function is
  `min(max_step, evidence_strength * (loosen_rate if direction ==
  "loosen" else tighten_rate))`, with `tighten_rate > loosen_rate` by a
  fixed multiple (proposed starting ratio 3:1 — tightening moves three
  times faster than loosening for the same evidence strength; this ratio
  itself is a hard-coded constant in `adaptive_params.py`, not something
  the system can adjust about itself).
- **Bounded step**: `max_step` is a hard-coded per-parameter fraction
  (e.g., `conviction_floor` moves at most 0.03 per adjustment), enforced
  inside `apply()` regardless of how strong the evidence looks.
- **Logged with evidence**: every `apply()` call writes an
  `adaptive_param_log` row with `old_value`, `new_value`, `sample_ids`
  (the exact `closed_trades`/`refusals` rows behind the move),
  `evidence_summary`, and `reverses_to` (the value a rollback would
  restore).
- **Reversible and auto-reverting**: `adaptive_params.py` runs a check on
  every new sample after an adjustment — if the next `MIN_SAMPLE_SIZE`
  window scores worse than the window before the change, it automatically
  writes a reverting `adaptive_param_log` entry back to `reverses_to` and
  flags it for dashboard visibility.

---

## 7. Cost governor

### 7.1 Why the governor cannot trust the Cost API for real-time gating

TRAPS.md is explicit: the Anthropic Cost API reports whole days only, and
today's spend is not queryable until the day closes. A governor that asked
the Cost API "how much have we spent today" before authorizing a call
would always see a number that understates reality by however much has
been spent since midnight — which defeats the purpose of a pre-call gate.

So `cost/governor.py` keeps its own running ledger, priced locally from
each response's raw `usage` object using the documented rules (cache
writes at 1.25x input, cache reads at 0.1x, web search at $10/1000 quer­ies
on top of tokens — TRAPS.md). The Cost API is used only for **retrospective
reconciliation**: once a day closes, `cost/tracker.py` compares its local
running total for that day against the Cost API's billed figure and
surfaces any discrepancy on the dashboard rather than silently trusting
either source.

### 7.2 The gate

```python
def authorize(estimate: CostEstimate, kind: Literal["scheduled", "manual"]) -> GovernorDecision:
    ...
    # cap = base_cap_cents (500, i.e. $5, per BUILD-BRIEF.md) +
    #       realized_profit_contribution
    # realized_profit_contribution = sum(
    #     max(0, trade.realized_pnl_cents) for trade in closed_trades_this_month
    # ) * governor_profit_share   # a small, bounded, itself-adaptive fraction
    #
    # month_to_date_scheduled_spend is read from the LOCAL ledger, never
    # the Cost API, for exactly the reason in §7.1.
    #
    # if kind == "scheduled" and (month_to_date_scheduled_spend + estimate)
    #     > cap: deny, log the skip and the shortfall
    # "manual" (ad hoc testing) spend is tracked in the same ledger but
    # under a SEPARATE running total and does not consume the scheduled cap
    # — see TRAPS.md "separate scheduled spend from manual spend."
```

Every skip is written to `cost_governor_events` with the estimate, the
cap, and the shortfall, so the dashboard can say "skipped: cost cap,
would have needed $X more" rather than the pipeline silently doing
nothing — the same "a zero is never left unexplained" principle applied to
budget refusals as to empty data results.

### 7.3 Where research/ calls into cost/

`research/boundary.py`'s `investigate()` calls `cost.governor.authorize()`
before making the Claude request (using a pre-call token estimate for
that candidate/prompt shape) and `cost.tracker.record()` immediately after,
using the response's raw usage object — never named fields alone, per
TRAPS.md's "store the raw usage object verbatim" rule. If `authorize()`
denies, `investigate()` returns a `ResearchCallLog` with
`skipped_reason="budget_denied"` and `parsed_view=None`; the candidate is
recorded as un-researched, not silently dropped from any count.

### 7.4 Annualizing is refused, not performed

Per TRAPS.md and the strategy-analyst brief item ("Annualising from a
short window is refused, not performed"), `cost/ledger.py` exposes no
function that multiplies a partial-month figure into an annual estimate.
The dashboard computes the annual hurdle only from a rolling window that
meets a minimum sample size (documented alongside the number, matching
`adaptive_params`'s pattern of stating and defending minimums).

---

## 8. File ownership table

Extends the brief's starting table (BUILD-BRIEF.md § "One owner per file")
with the modules this document adds. "Nobody else edits" holds except
where marked shared.

| Path | Owner | Nobody else edits | Notes |
|---|---|---|---|
| `data/` | `data-engineer` | yes | |
| `discovery/candidates.py` | `strategy-analyst` | yes | Candidate definition is a strategy decision — see §9.2. |
| `discovery/correlation.py` | `strategy-analyst`, reviewed by `risk-reviewer` | shared review, single owner for edits | Feeds directly into `risk.evaluate`'s concentration check; risk-reviewer must sign off on the clustering logic even though strategy-analyst authors it. |
| `research/prompts.py` | `strategy-analyst` | yes | What Claude is asked. |
| `research/schema.py` | **human-review-required** | yes | The `ResearchView` contract — this is the boundary object; any change needs risk-reviewer's sign-off per CLAUDE.md house rule 5. |
| `research/boundary.py` | **human-review-required** | yes | Enforcement code — see §4. |
| `risk/` (all files) | **human-review-required** | yes | Per BUILD-BRIEF.md's own ownership table. |
| `execution/` (all files) | **human-review-required** | yes | Per BUILD-BRIEF.md's own ownership table. |
| `cost/` | `cost-auditor` | yes | |
| `backtest/` | `backtest-engineer` | yes | |
| `storage/schema.sql` | **single coordinating session** | shared, no parallel edits | Per BUILD-BRIEF.md: "Database schema and configuration are touched by everyone — route every change to those through a single session, one at a time." |
| `dashboard/` | `ui-designer` | yes | |
| `orchestrator/` | `integration-engineer` | yes | Thin wiring only — calls the public interfaces in §3.2; if a change needs to touch another module's internals, that's a sign the interface is wrong and belongs back in this document's revision, not a local hack. |
| `install/` | `integration-engineer` | yes | |
| `tests/` | `test-writer`, `stress-tester` | shared, by file | Per BUILD-BRIEF.md's own ownership table. |
| `docs/ARCHITECTURE.md` (this file) | **single coordinating session** | shared, no parallel edits | Interface changes route back here first. |

`risk-reviewer` and `market-structure` have no write tools and can run in
parallel with anything, per the brief.

---

## 9. Significant decisions and rejected alternatives

**9.1 Event-sourced, append-only storage for decision data — rejected
mutable "current state" tables.** A mutable `positions` table updated in
place would be simpler to query but cannot answer "what did the model see
at the moment of the decision" once later cycles overwrite fields. The
brief's core requirement — reconstruct any trade after the fact — is
incompatible with mutation of the decision record; only the *position*
and *fill* tables mutate (status, price), never `research_calls` or
`risk_decisions`.

**9.2 `discovery/candidates.py` owned by `strategy-analyst`, not
`data-engineer`.** Rejected: folding candidate definition into the data
layer, reasoning that "which fields mean an event is tradeable" sounds
adjacent to "what does this field mean" (a data-engineer concern per their
brief). Rejected because *which* events count as candidates is a strategy
choice that must be graded on the backtest and re-decided as strategies
change — it is not a fixed interpretation of a source's schema the way
"a ClinicalTrials.gov completion date is not an announcement date" is.
Splitting them lets `data-engineer` keep adapters strategy-agnostic and
lets `strategy-analyst` iterate on candidate definitions without touching
source code.

**9.3 Research module split into `prompts.py` (strategy-analyst) and
`boundary.py` (human-review-required), not one owner.** Rejected: a single
owner for all of `research/`, since it's one conceptual pipeline stage.
Rejected because the enforcement code in `boundary.py` is exactly the kind
of code risk-reviewer's checklist item 1 exists to audit ("does a model
output reach a sizing calculation") — it must get that review regardless
of who writes the prompts, so splitting the file lets prompt iteration
happen without triggering a full risk review on every wording change,
while keeping the boundary code under the review gate every time.

**9.4 Conviction is a binary gate, never a size input — rejected
conviction-weighted or bucketed sizing.** A tempting middle ground: bucket
conviction into low/medium/high tiers and let a fixed, code-owned table
map bucket to size. Rejected because risk-reviewer's own checklist says
plainly: "If a model output reaches a sizing calculation, that is a
finding" — and a continuous or bucketed multiplication of conviction into
size is exactly that, even if the multiplier table itself is code-owned.
The safer and more auditable reading treats conviction as gating only;
sizing is a function of account equity, stop distance, and adaptive risk
budget alone, independent of how far above the floor conviction landed.

**9.5 Hard bounds isolated in a physically separate module with no writer,
not a single config file with a "hard" flag per field.** Rejected: one
`config/limits.yaml` with entries flagged `mutable: true/false`, checked
at write time. Rejected because a single shared config file is exactly
the shared-file collision risk the brief warns about, and a flag is a
runtime fact that a bug (or a careless future edit) can get wrong — it is
a check that could itself have a bug. Physical separation (different file,
no import path, human-only commit access, plus the runtime guard in §6.2)
makes the property closer to structurally guaranteed than
runtime-checked, with the runtime guard as a second line of defense rather
than the only one.

**9.6 Backtest has two modes — structural (free, no Claude) and judgement
(costed, calls the real `research.investigate`) — not one mode calling
Claude every run.** Rejected: always replaying the actual model call so
the backtest tests exactly what production runs. Rejected because
backtest-engineer's own brief requires it be "re-runnable at zero cost...
it will be run hundreds of times" — replaying Claude hundreds of times is
neither free nor fast, and is exactly the kind of expense the cost
governor exists to prevent even for testing. The structural mode (a
pluggable deterministic signal function, no network) is what "grade
several strategies" runs on; the judgement mode exists to periodically
validate that the model's contribution adds value over the raw signal,
run sparingly and tracked as manual spend.

**9.7 Correlation/concentration clustering lives in `risk.evaluate` via
`discovery.correlation.cluster()`, not as a discovery-time filter.**
Rejected: rejecting or down-weighting a candidate for correlation risk at
discovery time, before it's known what's currently held. Rejected because
concentration is a portfolio-level property — the same biotech candidate
is fine in isolation and dangerous as a fifth correlated position; only
`risk.evaluate`, which has `PortfolioState`, can judge that. Discovery
computes the clustering *key* (sector/type/date) once per candidate;
risk decides whether it collides with what's open.

**9.8 (restated from §4.3) Conviction gates, never sizes** — see 9.4
above; listed here too because it is the single most risk-relevant
decision in this document and risk-reviewer's findings (§10) speak to it
directly.

**9.9 The minimum-sample-size numbers in §6.1 are stated as provisional
starting estimates, not validated thresholds — rejected shipping them as
if backed by a power analysis.** It would be easy to write "30 trades"
with false confidence. The honest position, matching BUILD-BRIEF.md's own
"honest constraint" section, is that these numbers are placeholders
`backtest/` must validate or revise, and the dashboard must say plainly
that adaptation is far from having enough evidence to move anything for
months — presenting invented rigor here would recreate exactly the defect
the brief describes in the previous build ("both invented, neither
validated").

**9.10 The orchestrator is deliberately thin and owned by
`integration-engineer`, not left unowned as "just wiring."** Rejected:
no owner, on the theory that a thin sequencing file is low-risk enough
not to need one. Rejected because the brief's collision-avoidance section
is explicit that unowned shared-adjacent files are exactly how silent
overwrites happen — the orchestrator touches every module's public
interface, so it needs one clear owner even though its content should
rarely change once §3.2's signatures are stable.

---

## 10. Risk-reviewer's findings on the risk/execution boundary

`risk-reviewer` (read-only) was asked to examine this design's handling of
sizing, the Claude/code boundary, and the adaptive-parameter enforcement
described in §4 and §6. Findings, ranked by money at risk:

1. **Worst-case-after-loosening arithmetic is specified but not yet a
   number.** §6.2's runtime guard requires computing "the worst-case
   downstream effect... run through sizing's own worst-case calculation,"
   but this document does not state what that worst case actually is as a
   percentage of account. Before `risk/sizing.py` is implemented, compute
   the worst case after the maximum plausible sequence of loosening
   adjustments (three to five adjustments at `max_step`, per §6.3) and
   confirm it stays under `HardBounds.max_loss_per_position_pct`. If that
   number comes out worse than the hard bound once real step sizes are
   chosen, the bounds are not actually binding, and that must be visible
   on the dashboard, not discovered later.
2. **The conviction-as-gate-only decision (§4.3, §9.4) is correct, but
   its enforcement in this document is still descriptive, not testable.**
   Nothing in §3.2's signatures prevents a future `risk/sizing.py` author
   from adding a `conviction` parameter "just to use it more." Recommend
   the implementation phase pin this with a property test (test-writer's
   domain): construct many `ResearchView`s differing only in `conviction`,
   confirm `sizing` output is identical for a fixed `RiskDecision.action`
   and category. This is a five-minute test that makes the architectural
   promise in §4.3 self-enforcing rather than merely documented.
3. **`stop_price` sizing must be checked against a stop the system can
   actually enforce, and this document doesn't yet say which.** TRAPS.md
   is explicit that stop orders don't trigger outside regular hours and
   fractional stops expire at the close (`time_in_force=DAY`, must be
   re-placed each session). §3.1's `RiskDecision.stop_price` doesn't
   distinguish a resting broker stop from a polled one. If sizing is
   computed off stop distance without confirming the stop is actually
   resting at the broker for the full exposure window, the priced risk is
   not the real risk — this is risk-reviewer's own checklist item 2, and
   it needs an explicit answer before `risk/sizing.py` is written, not
   left implicit in `execution/orders.py`'s eventual implementation.
4. **The break-even arithmetic (return per trade needed to clear costs)
   is out of scope for this document by design (it's a strategy-analyst
   backtest output, not an architecture decision) but the interface must
   carry it forward.** `BacktestResult` (§3.2) should include this figure
   explicitly once implemented, or the dashboard cannot show it per
   BUILD-BRIEF.md's requirement — flagging here so it isn't lost between
   this document and the eventual `backtest/scoring.py` implementation.
5. **Kill switches are checked once per cycle before candidate evaluation
   (§3.2, `risk/kill_switches.py`) — confirmed correct, not a finding.**
   This satisfies risk-reviewer's checklist item 6, and correctly also
   forces exits rather than only blocking new entries once tripped
   (§2.1's `orchestrator/cycle.py` responsibility). Noting explicitly per
   the brief's instruction to "say so" when something survives review —
   this is a result, not silence.

None of these findings block merging this document — they are
implementation-phase requirements that this document's signatures already
make representable (the fields exist; the values and tests don't yet).
Recommend `risk-reviewer` re-review once `risk/sizing.py` and
`risk/kill_switches.py` have actual bodies, per CLAUDE.md house rule 5.

---

## 11. Open items deferred to implementation

Per §1's scope, this document fixes interfaces and ownership, not values.
Explicitly still open, each owned by the module that will resolve it:

- Exact `HARD_BOUNDS` values (human decision, informed by
  `backtest/structural.py` results) — **human decides, not this document.**
- Starting `adaptive_params` values and the `tighten_rate`/`loosen_rate`
  ratio beyond the 3:1 starting proposal in §6.3 — `strategy-analyst` +
  `risk-reviewer`, backed by backtest evidence.
- Which strategies discovery/research actually implement — entirely open
  per BUILD-BRIEF.md, resolved by the strategy bake-off (build order step 3).
- Runtime API budget figure — proposed and justified by whoever owns the
  strategy that wins the bake-off, against this cost governor's numbers.
