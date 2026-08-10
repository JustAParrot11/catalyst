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
class UsageComponents:
    # Mirrors the raw Anthropic usage object field-for-field. TRAPS.md:
    # cache tokens are billed but excluded from input_tokens; missing
    # them understates the bill by about half. No pricing code may read
    # input_tokens/output_tokens alone — cost.tracker.price() (§3.2)
    # is the only function permitted to turn this into a dollar figure,
    # and it must read every field below, not a subset.
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    web_search_requests: int
    raw: dict                        # the verbatim API usage object, always

@dataclass(frozen=True)
class APITurn:
    # One investigation may span several Anthropic API requests: zero or
    # more exploration turns (tool_choice="auto", may call web_search)
    # followed by exactly one forced-choice extraction turn (§4.2). Each
    # turn is priced independently and summed — never just the last one.
    turn_index: int
    raw_response: dict               # entire API response, verbatim
    usage: UsageComponents
    stop_reason: str

@dataclass(frozen=True)
class ResearchCallLog:
    id: str
    candidate_id: str
    model: str
    prompt_rendered: str             # exact prompt sent, for replay/audit
    tools_offered: list[str]
    api_turns: list[APITurn]         # every API request this investigation
                                      # made, in order — see APITurn above
    parsed_view: ResearchView | None # None if the call was skipped/failed
    cost_cents: Decimal              # == sum(tracker.price(t.usage) for t in api_turns)
    latency_ms: int
    skipped_reason: str | None       # e.g. "budget_denied", "api_error"


# --- cost/ shapes, used by research/boundary.py and backtest/judgement.py ---

@dataclass(frozen=True)
class CostEstimate:
    estimated_cents: Decimal
    basis: str                       # e.g. "candidate_prompt_shape_v3"
    kind: Literal["scheduled", "manual"]
    component: str                   # "research", "backtest_judgement", ...

@dataclass(frozen=True)
class CostEvent:
    id: str
    usage: UsageComponents
    kind: Literal["scheduled", "manual"]
    component: str
    priced_cents: Decimal
    priced_at: datetime
    api_call_id: str | None          # None for pre-response accounting

@dataclass(frozen=True)
class GovernorDecision:
    authorized: bool
    kind: Literal["scheduled", "manual"]
    estimate: CostEstimate
    cap_cents: Decimal
    period_to_date_cents: Decimal    # for this kind only — never pooled
    shortfall_cents: Decimal | None  # populated only when authorized=False
    reason: str | None

@dataclass(frozen=True)
class CostContext:
    # Carried by orchestrator/cycle.py (scheduled cycles) or
    # backtest/judgement.py (manual) into research.investigate() so the
    # governor tags the call with the right kind/component before pricing.
    kind: Literal["scheduled", "manual"]
    component: str


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


# --- risk/adaptive_params.py shapes ---

@dataclass(frozen=True)
class EvidenceSample:
    parameter: str
    trade_ids: list[str]              # closed_trades.position_id values,
                                       # OR refusals.decision_id values —
                                       # never anything from unrealized P&L
    window_start: datetime
    window_end: datetime              # must not overlap the window that
                                       # justified this parameter's last
                                       # adjustment (§6.3, apply())
    effect_size: Decimal              # e.g. mean realized-return delta
                                       # between the current setting and
                                       # what the evidence implies
    significance: Decimal             # e.g. p-value or equivalent
                                       # confidence measure on effect_size
    evidence_strength: Decimal        # a single scalar derived ONLY from
                                       # effect_size + significance + the
                                       # sample count — never from a
                                       # model's stated confidence or from
                                       # anything in ResearchView. This is
                                       # the value apply()'s step-size
                                       # function (§6.3) scales against.

@dataclass(frozen=True)
class AdjustmentProposal:
    parameter: str
    direction: Literal["tighten", "loosen"]
    old_value: Decimal
    proposed_value: Decimal           # already step-bounded per §6.3
                                       # before apply() even runs
    evidence: EvidenceSample
    applicable: bool                  # False if below MIN_SAMPLE_SIZE
    reason: str | None                # populated when applicable=False


# --- execution/ -> storage/ ---

@dataclass(frozen=True)
class OrderResult:
    decision_id: str
    broker_order_id: str | None       # None if the broker rejected before
                                       # assigning an ID
    status: str
    submitted_at: datetime
    raw_response: dict                # the broker's verbatim response,
                                       # always — including on rejection.
                                       # A rejected or malformed stop is
                                       # exactly the failure that leaves a
                                       # position unprotected; diagnosing
                                       # it after the fact must not require
                                       # SSH access to a log file, matching
                                       # how raw_events_errors and
                                       # research_calls already carry the
                                       # raw upstream response beside a
                                       # failure (§5).

@dataclass(frozen=True)
class Fill:
    order_id: str
    price: Decimal
    qty: Decimal
    filled_at: datetime
    broker_reported_price: Decimal   # kept distinct from any modeled price

@dataclass(frozen=True)
class StopReplacementResult:
    position_id: str
    old_stop_order_id: str | None
    new_stop_order_id: str | None     # None if replacement failed —
                                       # never populated unless the old
                                       # stop's cancellation was confirmed
    status: Literal["replaced", "failed_cancel_unconfirmed"]
    raw_response: dict

@dataclass(frozen=True)
class StopConfirmation:
    position_id: str
    live_stop_order_ids: list[str]    # queried fresh from the broker,
                                       # not read from positions.stop_order_id
    status: Literal["ok", "unprotected", "duplicate_stops"]


# --- backtest/ -> storage/ ---

@dataclass(frozen=True)
class BacktestResult:
    strategy_name: str
    date_range: tuple[date, date]
    in_sample: BacktestSampleStats
    out_of_sample: BacktestSampleStats
    costs_applied: dict               # spread/slippage/API-cost assumptions
                                       # used, stated explicitly per
                                       # backtest-engineer's own brief
    market_regime_notes: str          # what was happening in the market
                                       # during date_range

@dataclass(frozen=True)
class BacktestSampleStats:
    sample_size: int
    hit_rate: Decimal
    mean_return: Decimal
    median_return: Decimal
    worst_single_outcome: Decimal
    max_drawdown: Decimal
    return_per_trade_needed_to_break_even: Decimal  # against $1,000
                                                      # account + the
                                                      # governor's cap —
                                                      # the dashboard's
                                                      # annual-hurdle
                                                      # comparison reads
                                                      # this field
```

**Minor plumbing types referenced above but not spelled out** —
`PortfolioState`, `Position`, `MarketSnapshot`, `KillSwitchState`,
`ExitAction`, `PointInTimeData`, `AdaptiveParamLogEntry`,
`ReconciliationResult` — are straightforward reads of fields already
defined elsewhere in this section (open positions and equity for
`PortfolioState`; volatility/spread for `MarketSnapshot`; a boolean plus
a reason string for `KillSwitchState`, mirroring `GovernorDecision`'s
shape) and are left to implementation rather than fully spelled out here.
This is a deliberate line, not an oversight: every type that sits on the
model-boundary or the money path — `ResearchView`, `RiskDecision`,
`SizingResult`, `HardBounds`, `EvidenceSample`, `UsageComponents`,
`CostEstimate`/`CostEvent`/`GovernorDecision`, `OrderResult`,
`StopReplacementResult` — is fully specified above; the plumbing types
are not.

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
    # silently does nothing. May run one or more exploration turns
    # (tool_choice="auto") before exactly one forced-tool_choice
    # extraction turn against the ResearchView JSON schema (see §4.2); no
    # other code path produces a ResearchView. Every turn's usage is
    # captured in api_turns and priced independently (see §7.3).

# risk/evaluate.py
def evaluate(
    candidate: Candidate,
    view: ResearchView,
    portfolio: PortfolioState,
    params: AdaptiveParamSnapshot,   # read-only snapshot, see §6
) -> RiskDecision: ...
    # evaluate() reads exactly one field of `view` for anything that
    # reaches sizing: it compares view.conviction against
    # params.conviction_floor to produce a plain bool, `passed_gate`.
    # That bool — not `view` — is what gets passed into sizing.size()
    # below. No other field of `view` (direction's certainty, priced_in,
    # expected_holding_days, ...) is readable by sizing at all, because
    # sizing's signature has no parameter shaped to receive them. This
    # closes the gap risk-reviewer flagged: the object that reaches the
    # arithmetic-producing function cannot carry ResearchView's other
    # fields even if a future edit tried to pass them in. See §4.3/§9.4.

# risk/sizing.py — the ONLY function in the system permitted to
# construct a notional_usd or qty value.
def size(
    passed_gate: bool,               # from evaluate() — see above; the
                                       # sole trace of the model's output
                                       # that reaches this function
    catalyst_type: str,               # from Candidate — not from Claude
    portfolio: PortfolioState,
    params: AdaptiveParamSnapshot,
    hard_bounds: HardBounds,
    market: MarketSnapshot,           # volatility, spread — independent
                                       # of anything Claude said
) -> SizingResult: ...
    # Sizes off max(params.adverse_gap_assumption[catalyst_type],
    # nominal stop distance implied by market), never off nominal stop
    # distance alone — stop orders do not trigger outside regular hours
    # and fractional DAY stops expire at the close (TRAPS.md), so the
    # nominal distance is not the real worst case for the catalyst types
    # this system targets. If passed_gate is False, returns a "skip"
    # SizingResult with no notional/qty computed at all.

@dataclass(frozen=True)
class SizingResult:
    action: Literal["trade", "skip"]
    notional_usd: Decimal | None
    qty: Decimal | None
    stop_price: Decimal | None
    limits_applied: list[LimitApplication]
    skip_reasons: list[str]

# risk/kill_switches.py
def check(portfolio: PortfolioState, hard_bounds: HardBounds) -> KillSwitchState: ...
    # Called once per cycle, before any candidate is evaluated. If
    # tripped, the orchestrator skips straight to execution.manage_exits()
    # and blocks new entries for the rest of the cycle. FAILS CLOSED: if
    # `portfolio` cannot be built from a reliable broker/data read (stale
    # data, broker API error), check() returns KillSwitchState.tripped =
    # True with reason="portfolio_state_unreliable" — it never proceeds
    # as "not tripped" on a data failure. Losing exactly when something
    # is already wrong is the failure mode this closes off.

# risk/adaptive_params.py
# (signatures as implemented at stage 5: propose_adjustment gained
# current_value - the proposal must be computed against the live value
# it will adjust; apply gained conn - the log table IS the adaptive
# store, and the disjoint-window + closed-outcome provenance checks
# read it. Both deviations recorded in the stage-5 PR.)
def propose_adjustment(parameter: str, current_value: Decimal,
                       evidence: EvidenceSample) -> AdjustmentProposal: ...
def apply(
    proposal: AdjustmentProposal,
    hard_bounds: HardBounds,
    current_snapshot: AdaptiveParamSnapshot,  # the full live state of
                                                # every OTHER adaptive
                                                # parameter, not a fixed
                                                # baseline
    conn: sqlite3.Connection,
) -> ApplyOutcome: ...
    # apply() re-checks the proposal against hard_bounds itself — it does
    # not trust the caller to have checked. Critically, the worst-case
    # simulation runs against current_snapshot with ONLY the proposed
    # parameter changed — a JOINT check against every other parameter's
    # current (already-adjusted) value, never a marginal check of the
    # proposed value in isolation. This closes the compounding gap
    # risk-reviewer flagged: stop_width and adverse_gap_assumption can
    # each individually pass a marginal check while their combination
    # breaches a hard bound; a joint check against live state catches
    # that. apply() also refuses a proposal whose evidence window
    # overlaps the window that justified this parameter's previous
    # adjustment — each adjustment needs its own disjoint batch of
    # closed-trade evidence, not a rolling window re-firing on nearly the
    # same data. See §6.3.

# execution/orders.py
def place(decision: RiskDecision) -> OrderResult: ...
def replace_stop(position: Position, new_stop_price: Decimal) -> StopReplacementResult: ...
    # The required daily re-arm for fractional DAY stops (TRAPS.md).
    # Sequencing is cancel-then-confirm-then-place: the old stop's
    # cancellation must be confirmed by the broker before the new stop is
    # submitted. If cancellation confirmation fails or times out,
    # replace_stop() does NOT place the new stop — it returns a failure
    # result and the position is flagged for the check below, rather than
    # risking two live stops on one position.
def confirm_stops_resting(positions: list[Position]) -> list[StopConfirmation]: ...
    # Run once per session at the open (orchestrator/cycle.py). For every
    # open position, queries the broker directly for open stop-type
    # orders against that position — not `positions.stop_order_id` alone,
    # since that field can be stale — and flags any position with zero
    # or more than one live stop order. A position with zero confirmed
    # stops is treated as unprotected: orchestrator/cycle.py blocks new
    # entries and surfaces it on the dashboard until resolved.

# execution/reconcile.py
def reconcile() -> list[Fill]: ...
# execution/exits.py
def manage_exits(portfolio: PortfolioState, as_of: datetime) -> list[ExitAction]: ...

# cost/governor.py
def authorize(estimate: CostEstimate) -> GovernorDecision: ...
    # estimate.kind selects the cap checked: "scheduled" against
    # base_cap_cents + realized_profit_contribution (§7.2); "manual"
    # against a separate, human-set MANUAL_SPEND_CAP_CENTS_PER_MONTH
    # constant. Both branches are checked — "separated" does not mean
    # "unbounded" for manual spend (closes the gap cost-auditor flagged;
    # see §7.2).
# cost/tracker.py
def record(event: CostEvent) -> None: ...
def price(usage: UsageComponents) -> Decimal: ...
    # The ONLY function permitted to convert a usage object into a dollar
    # figure. Reads input_tokens, output_tokens, cache_creation_input_tokens,
    # cache_read_input_tokens, AND web_search_requests explicitly — there
    # is no code path that prices from input_tokens/output_tokens alone.
    # See UsageComponents in §3.1: the type itself carries all five
    # fields, so an implementation that reads a subset is a visible bug,
    # not a silent one. test-writer must add a fixture usage object with
    # every field populated and assert the priced total changes when each
    # one changes independently — see §10.
def reconcile_day(target_date: date, kind: Literal["scheduled", "manual"], component: str) -> ReconciliationResult: ...
    # Queries the Cost API for exactly ONE closed day at a time — never a
    # multi-day range — sidestepping the default-page-size trap (TRAPS.md)
    # by construction. Parses the Cost API's amount as a decimal-string
    # count of CENTS, matching this system's own cents-everywhere
    # convention (see all *_cents fields in §3.1/§5), and compares it
    # against the local ledger total for that exact (date, kind,
    # component) triple — never a pooled total across kinds. On
    # discrepancy beyond a configured threshold, does not just log a
    # dashboard flag: it also pauses new "scheduled" authorizations until
    # a human acknowledges the reconciliation event (see §7.1). If a
    # backfill path is ever added for a multi-day catch-up (e.g. after
    # downtime), it must take an explicit page-size/limit parameter sized
    # to the window — never rely on the API's default page size.

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
model writes lives in `ResearchCallLog.api_turns[i].raw_response` for the
audit trail (§5) but no code path reads it for anything numeric. If
Claude's prose happens to contain something that looks like a size ("a
$200 position seems reasonable"), it never reaches an arithmetic
operation, because nothing downstream of `investigate()` reads
`raw_response` except the dashboard's narrative view.

A third layer, closing a gap risk-reviewer identified in this document's
first draft (§10 finding 3), sits at the `evaluate()` → `sizing.size()`
boundary itself: `sizing.size()`'s signature (§3.2) has no parameter
capable of receiving a `ResearchView` at all — only `passed_gate: bool`,
the candidate's `catalyst_type`, and inputs independent of anything Claude
said. The first draft passed the full `view` object into `evaluate()` and
argued by *convention* that only `conviction` would ever be read from it;
that argument held only as long as nobody changed `sizing.py` to read
another field. The fix makes it a type error, not a promise: even a
careless future edit to `sizing.py` cannot read `expected_holding_days` or
`priced_in` from something it was never handed.

### 4.2 Enforcing the schema at the API layer

`research/boundary.py`'s `investigate()` may run zero or more *exploration*
turns first — `tool_choice: "auto"`, with search/fetch-style tools
available so Claude can gather information — followed by exactly one
*extraction* turn where `tool_choice` is forced to a single tool,
`submit_research_view`, whose `input_schema` is generated from
`ResearchView`'s field set (`direction`, `conviction`, `thesis`,
`invalidation`, `expected_holding_days`, `priced_in`,
`priced_in_reasoning` — nothing else). This is standard Anthropic
tool-use: forcing `tool_choice` to one tool on that final turn means the
API's only valid completion is a call to that tool, so `investigate()` can
assert `response.stop_reason == "tool_use"` on the extraction turn and
parse `tool_use.input` directly into `ResearchView` without ever touching
a free-text block for the canonical result. Every turn — exploration and
extraction alike — is recorded as an `APITurn` in
`ResearchCallLog.api_turns` (§3.1) and priced independently (§7.3), so a
multi-turn investigation's cost is never computed from only the final
turn's usage.

### 4.3 Conviction gates; it does not size

`risk/evaluate.py` uses `view.conviction` exactly once: compared against
`adaptive_params.conviction_floor` to produce a boolean trade/no-trade
gate, `passed_gate`. It is never multiplied into a size, never bucketed
into a size tier, and never otherwise touches `sizing.py`'s arithmetic —
and as of §4.1's third layer, it structurally cannot, because
`sizing.size()` only accepts the boolean, not the view. Approved trades of
the same risk category are sized identically regardless of whether
conviction was 0.66 or 0.99 above the floor. See §9.8 for why this
stricter reading was chosen over a conviction-weighted or bucketed
alternative.

### 4.4 What "code decides" covers, concretely

Everything downstream of the gate is `risk/sizing.py` reading only:
`passed_gate`, `catalyst_type`, account equity (via `portfolio`),
`adaptive_params_snapshot` (stop widths, adverse-gap assumptions by
catalyst type), `hard_bounds` (max loss per position, max exposure), and
`market` data (volatility, spread) fetched independently of anything
Claude said. Sizing is computed off `max(adverse_gap_assumption for that
catalyst_type, nominal stop distance)`, never off nominal stop distance
alone — TRAPS.md is explicit that stop orders do not trigger outside
regular hours and that fractional DAY stops expire at the close and must
be re-armed every session (see `execution/orders.py`'s
`confirm_stops_resting()` in §3.2), so the nominal stop distance
understates the real overnight worst case for exactly the catalyst types
this system is likely to target (binary readouts). `RiskDecision.
limits_applied` records every rule that touched the outcome and whether
it bound, so the dashboard can show "the model rated this 0.81 conviction;
code capped it at $180 because of the sector-concentration limit" as a
factual reconstruction, not a narrative.

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
                        tools_offered, cost_cents, latency_ms,
                        skipped_reason, called_at)
research_call_turns   (call_id, turn_index, raw_response, usage_raw,
                        stop_reason)  -- one row per APITurn (§3.1); an
                                       -- investigation with N exploration
                                       -- turns + 1 extraction turn has
                                       -- N+1 rows here, and cost_cents on
                                       -- research_calls == the sum of
                                       -- tracker.price() over every row
research_views        (candidate_id, direction, conviction, thesis,
                        invalidation, expected_holding_days, priced_in,
                        priced_in_reasoning)

-- risk/ layer
risk_decisions         (candidate_id, action, side, notional_usd, qty,
                        stop_price, planned_exit_date, skip_reasons,
                        adaptive_params_snapshot, decided_at)
                        -- populated from evaluate()'s passed_gate result
                        -- composed with sizing.size()'s SizingResult
                        -- (§3.1/§3.2) plus candidate_id and the snapshot
limit_applications     (decision_id, rule_name, bound_value,
                        requested_value, bound_type, binding)
refusals               (decision_id, candidate_id, price_at_refusal,
                        refused_at, scored_at, outcome_price,
                        outcome_return)  -- scored_at/outcome_* filled by
                                          -- an async job days/weeks later
kill_switch_events     (triggered_at, switch_name, portfolio_state_snapshot,
                        cleared_at)  -- switch_name = "portfolio_state_unreliable"
                                      -- records the fail-closed case (§3.2)
                                      -- exactly like any other trip
adaptive_param_log     (parameter, old_value, new_value, sample_ids,
                        evidence_window_start, evidence_window_end,
                        evidence_summary, changed_at, reverses_to,
                        reverted_at)  -- window columns are what apply()
                                       -- checks for the disjoint-evidence
                                       -- rule before allowing another
                                       -- adjustment to the same parameter

-- execution/ layer
orders                (id, decision_id, broker_order_id, side, qty,
                        order_type, time_in_force, submitted_at, status,
                        raw_response)  -- broker's verbatim response,
                                        -- present even on rejection
stop_replacements      (position_id, old_stop_order_id, new_stop_order_id,
                        status, raw_response, replaced_at)  -- one row per
                        -- daily re-arm attempt (TRAPS.md); status =
                        -- "failed_cancel_unconfirmed" is how a
                        -- near-miss on two-live-stops becomes visible
stop_confirmations     (position_id, checked_at, live_stop_order_ids,
                        status)  -- one row per open-of-session check;
                        -- status = "unprotected" or "duplicate_stops"
                        -- is what blocks new entries per confirm_stops_resting()
fills                  (order_id, price, qty, filled_at,
                        broker_reported_price, modeled_slippage)
positions              (id, ticker, entry_order_ids, stop_order_id,
                        opened_at, planned_exit_date, status)
closed_trades          (position_id, entry_price, exit_price, exit_reason,
                        realized_pnl_cents, expected_holding_days,
                        actual_holding_days, closed_at)

-- cost/ layer
cost_events             (raw_usage_json, kind, component, priced_cents,
                        priced_at, api_call_id)
cost_governor_events    (cycle_id, requested_kind, estimate_cents,
                        cap_cents, decision, reason, at)
cost_reconciliation_events (id, target_date, kind, component,
                        local_total_cents, cost_api_total_cents,
                        discrepancy_cents, threshold_cents, action_taken,
                        acknowledged_by, acknowledged_at, reconciled_at)
                        -- one row per (date, kind, component) reconcile_day()
                        -- call (§3.2/§7.1); action_taken records whether
                        -- scheduled authorization was paused

-- backtest/ layer
backtest_results        (id, strategy_name, mode, date_range_start,
                        date_range_end, costs_applied, market_regime_notes,
                        created_at)  -- mode = "structural" | "judgement" (§9.6)
backtest_sample_stats   (result_id, sample_kind, sample_size, hit_rate,
                        mean_return, median_return, worst_single_outcome,
                        max_drawdown, return_per_trade_needed_to_break_even)
                        -- sample_kind = "in_sample" | "out_of_sample";
                        -- this table is what the dashboard's annual-hurdle
                        -- comparison against S&P/T-bills reads from
```

Every "empty" query result the dashboard shows carries the raw upstream
response beside it (a `raw_events_errors` row, a `research_calls` row with
`skipped_reason` set, or an `orders`/`stop_replacements` row's
`raw_response` on rejection) — this satisfies CLAUDE.md house rule 3
("every zero gets its raw upstream response printed beside it") at the
schema level rather than relying on the UI layer to remember to fetch it.

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
    "governor_profit_share",         # cost/governor.py's cap-growth
                                      # fraction (§7.2) — cost-auditor
                                      # flagged that this lever controls
                                      # how fast the SPENDING CAP ITSELF
                                      # grows and was previously outside
                                      # every safeguard in §6.3; it is
                                      # governed by the identical regime
                                      # as every other adaptive parameter,
                                      # not a special case cost/ owns alone
]

MIN_SAMPLE_SIZE = {
    "conviction_floor": 30,          # closed, scored trades
    "adverse_gap_assumption": 20,    # per catalyst type
    "stop_width": 20,
    "holding_period_estimate": 15,
    "search_budget_allocation": 40,
    "governor_profit_share": 20,     # closed trades this month, resets
                                      # monthly per the cap's own period
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
   dashboard says which bound stopped it and by how much." **This check is
   joint, not marginal**: per §3.2, `apply()` takes the full
   `current_snapshot` of every *other* adaptive parameter's live value and
   simulates the proposed change against that whole snapshot, not against
   a fixed baseline. Two parameters that each individually pass a
   marginal check can still combine to breach a hard bound —
   `stop_width` and `adverse_gap_assumption` loosening together is the
   concrete example risk-reviewer raised — and only a joint check against
   live state catches that combination before it ships. `apply()` also
   refuses a proposal whose `EvidenceSample.window_start/window_end`
   overlaps the window recorded in `adaptive_param_log` for this
   parameter's previous adjustment, so a parameter cannot re-fire on
   nearly the same evidence via a rolling window.

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

So `cost/governor.py` keeps its own running ledger, priced by
`cost/tracker.py:price()` (§3.2) from each response's `UsageComponents`
(§3.1) — a type that carries `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`, and
`web_search_requests` as named fields, not a dict a pricing function
could silently under-read. Pricing follows the documented rules (cache
writes at 1.25x input, cache reads at 0.1x, web search at $10/1000
queries on top of tokens — TRAPS.md).

The Cost API is used only for **retrospective reconciliation**, via
`cost.tracker.reconcile_day()` (§3.2): once a day closes,
`reconcile_day()` queries the Cost API for **exactly that one day** —
never a multi-day range, which sidesteps the "default page size quietly
drops the newest days" trap by construction rather than by remembering to
pass a `limit` — parses its amount as a **decimal-string count of
cents** (matching this system's own cents-everywhere convention, and the
specific parsing trap TRAPS.md calls out), and compares it against the
local ledger's total for that **exact `(date, kind, component)` triple**,
never a total pooled across kinds or components. A bug that mis-prices
`kind="scheduled"` while over-pricing `kind="manual"` can net to zero in
an aggregate comparison and never surface; comparing per-kind closes
that. Every comparison writes a `cost_reconciliation_events` row (§5)
regardless of outcome. On a discrepancy beyond a configured threshold,
the response is not just a dashboard flag: `reconcile_day()` also pauses
new `kind="scheduled"` authorizations until a human acknowledges the
`cost_reconciliation_events` row — a silent, passive flag is the same
failure mode BUILD-BRIEF describes happening for days in a previous
build ("understated its bill by half... looked healthy the whole time");
requiring acknowledgment forces a human to actually look. If a
multi-day backfill path is ever added (service down for a stretch,
reconciling several days at once), it must take an explicit page-size
parameter sized to the window — the single-day-at-a-time default is not
a guarantee that covers every future code path, only the common one.

### 7.2 The gate

```python
def authorize(estimate: CostEstimate) -> GovernorDecision:
    ...
    # estimate.kind selects which cap is checked:
    #
    # kind == "scheduled":
    #   cap = base_cap_cents (500, i.e. $5, per BUILD-BRIEF.md) +
    #         realized_profit_contribution
    #   realized_profit_contribution = max(0, sum(
    #       trade.realized_pnl_cents for trade in closed_trades_this_month
    #   )) * governor_profit_share
    #   # NET monthly realized P&L, floored at zero for the MONTH as a
    #   # whole — not per trade. A month with one $50 winner and one $80
    #   # loser nets to -$30 and contributes nothing to the cap; flooring
    #   # each trade individually before summing would let losers vanish
    #   # and let winners inflate the cap on a month that was net-negative,
    #   # which is not what BUILD-BRIEF's "a fraction of realised profit"
    #   # means on a plain reading.
    #   #
    #   # period_to_date_cents is read from the LOCAL ledger for kind=
    #   # "scheduled" only, never the Cost API, for the reason in §7.1.
    #   #
    #   # if period_to_date_cents + estimate.estimated_cents > cap:
    #   #     deny, log cost_governor_events with the shortfall
    #
    # kind == "manual":
    #   cap = MANUAL_SPEND_CAP_CENTS_PER_MONTH   # a separate, human-set,
    #   # NON-adaptive constant in cost/governor.py. "Manual spend is
    #   # tracked separately" (TRAPS.md) means separately ACCOUNTED, not
    #   # UNBOUNDED — the backtest judgement mode (§9.6) routes through
    #   # this exact branch and, run over a large historical universe,
    #   # could otherwise spend past any figure with nothing to stop it.
    #   # This cap is what stops that.
    #   if period_to_date_cents (kind="manual") + estimate.estimated_cents
    #       > cap: deny, log the shortfall exactly as the scheduled branch does
```

Every skip is written to `cost_governor_events` with the estimate, the
cap, and the shortfall, so the dashboard can say "skipped: cost cap,
would have needed $X more" rather than the pipeline silently doing
nothing — the same "a zero is never left unexplained" principle applied to
budget refusals as to empty data results.

### 7.3 Where research/ (and backtest/) call into cost/

`research/boundary.py`'s `investigate()` calls `cost.governor.authorize()`
before **every** API turn it makes (using a pre-call token estimate for
that turn's prompt shape) and `cost.tracker.record()` immediately after
each one, using that turn's raw `UsageComponents` — never named fields
alone, per TRAPS.md's "store the raw usage object verbatim" rule.
Because an investigation can span several turns (§4.2), `cost_cents` on
the resulting `ResearchCallLog` is the **sum** of `price()` over every
`APITurn`, not just the final extraction turn's usage — a multi-turn
investigation that explores with `web_search` before producing its
verdict has its exploration cost counted, not silently dropped. If
`authorize()` denies any turn, `investigate()` stops there and returns a
`ResearchCallLog` with `skipped_reason="budget_denied"` and
`parsed_view=None`; whatever turns already ran are still priced and
recorded, and the candidate is marked un-researched, not silently
dropped from any count.

`backtest/judgement.py` (§9.6) reuses this exact `investigate()` function
— it does not have a separate, parallel cost-accounting path — so
judgement-mode backtest calls are tagged `kind="manual"` via
`CostContext` and are checked against `MANUAL_SPEND_CAP_CENTS_PER_MONTH`
(§7.2) like any other manual spend, not exempted because they originate
from `backtest/` rather than a live cycle.

### 7.4 Annualizing is refused, not performed

Per TRAPS.md and the strategy-analyst brief item ("Annualising from a
short window is refused, not performed"), `cost/ledger.py` exposes no
function that multiplies a partial-month figure into an annual estimate.
The dashboard computes the annual hurdle only from a rolling window that
meets a minimum sample size (documented alongside the number, matching
`adaptive_params`'s pattern of stating and defending minimums). The exact
minimum window is not yet chosen — tracked explicitly in §12's open
items, alongside `MIN_SAMPLE_SIZE`'s other placeholder values, so it
isn't quietly forgotten the way cost-auditor found it initially missing
from that list.

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

**9.11 `risk/sizing.py` takes a narrow, gate-only signature — rejected
passing the full `ResearchView` with a documentation-only promise to
read just `conviction`.** The first draft of this document did exactly
that, on the reasoning that §4.3's prose was clear enough about which
field mattered. risk-reviewer's finding 3 (§10) is why that was rejected:
a promise in prose is not enforcement, and a careless future edit to
`sizing.py` reading `expected_holding_days` or `priced_in` into the size
arithmetic would violate risk-reviewer's own checklist rule 1 without
violating the letter of anything written down. Passing only
`passed_gate: bool` makes the violation a type error instead of a
possibility.

**9.12 `adaptive_params.apply()`'s worst-case check is joint, against the
full live snapshot of every other parameter — rejected a marginal check
against a fixed baseline.** A marginal check (does this one proposed
value, alone, stay under the hard bound) is cheaper to compute and was
the first draft's implicit design. Rejected because two parameters can
each individually pass a marginal check while their *combination*
breaches a bound neither one breached alone — `stop_width` and
`adverse_gap_assumption` loosening together is the concrete case
risk-reviewer named (§10, finding 2) — and a marginal check cannot catch
that by construction, only a joint one can.

**9.13 The cost governor's realized-profit contribution nets the whole
month's P&L before flooring at zero — rejected flooring each trade
individually before summing.** Flooring per trade (`sum(max(0,
trade.pnl) for trade in month)`) lets a month with one winner and one
larger loser still grow the spending cap on a *net-losing* month, because
the loser's negative contribution is discarded before the sum. Rejected
because BUILD-BRIEF's "a fraction of *realised* profit" reads most
plainly as the period's net figure — cost-auditor's finding (§11)
prompted this and the two readings produce materially different caps on
a mixed month, so this document commits to the net interpretation
explicitly rather than leaving it to whichever engineer implements
`cost/governor.py` to guess.

**9.14 `risk/kill_switches.check()` fails closed on an unreliable
portfolio read — rejected failing open (proceeding as "not tripped").**
Failing open is simpler to implement and was unspecified, not chosen, in
the first draft — risk-reviewer's finding 6 (§10) named the gap.
Rejected because a kill switch exists precisely to catch things going
wrong, and a broker/data outage is itself evidence something may be
wrong; treating "I cannot verify the account is safe" as equivalent to
"the account is safe" inverts the switch's purpose at exactly the moment
it matters most.

---

## 10. Risk-reviewer's findings on the risk/execution boundary

`risk-reviewer` (read-only, model `sonnet`) independently reviewed this
design's handling of sizing, the Claude/code boundary, and the
adaptive-parameter enforcement described in §4 and §6, against its own
checklist. Findings below are the actual review output, ranked by money
at risk, each marked with what this document now does about it. Full
transcript available in the session; this is a faithful condensation.

1. **Worst-case-per-position was priced off a stop the design never
   confirmed was enforceable, and the one concrete example number (10%)
   was already at the danger line.** TRAPS.md: stops don't trigger
   outside regular hours, and fractional DAY stops expire at the close
   and must be re-armed every session. The original draft didn't state
   whether sizing used the gap-adjusted assumption or the nominal stop
   distance, and had no daily "is the stop actually resting" check.
   **[FIXED]** — `sizing.size()` (§3.2) now sizes off
   `max(adverse_gap_assumption[catalyst_type], nominal stop distance)`,
   never nominal distance alone (§4.4); `execution/orders.py` gained
   `confirm_stops_resting()`, run once per session at the open, which
   blocks new entries and surfaces the dashboard if any position has zero
   confirmed live stops (§3.2, §5's `stop_confirmations` table).
2. **The adaptive-parameter runtime guard's worst-case check might be
   marginal rather than joint — exactly the "lucky run loosens the
   limits" failure CLAUDE.md warns about — and no cadence rule stopped a
   parameter re-firing on overlapping evidence.** `stop_width` and
   `adverse_gap_assumption` could each individually pass a check against
   a fixed baseline while their *combination* breached a hard bound
   neither one breached alone. **[FIXED]** — `adaptive_params.apply()`
   (§3.2) now takes the full live `AdaptiveParamSnapshot` of every other
   parameter and simulates the proposed change jointly against it (§6.2);
   `apply()` also refuses a proposal whose evidence window overlaps the
   window that justified this parameter's previous adjustment, enforced
   via `adaptive_param_log.evidence_window_start/end` (§5, §6.2).
3. **The Claude→code firewall was real for `ResearchView`'s size fields,
   but not yet real for the rest of the object — and `risk/sizing.py` had
   no defined signature at all.** `evaluate()`'s original signature
   passed the *entire* `ResearchView` into risk logic; nothing but
   convention stopped a future edit from reading `expected_holding_days`
   or `priced_in` into the size arithmetic, which would violate
   risk-reviewer's own checklist rule 1 even without violating the
   letter of §9.4. **[FIXED]** — `sizing.size()` now has an explicit
   signature (§3.2) whose only trace of the model's output is
   `passed_gate: bool`; it has no parameter capable of receiving a
   `ResearchView` at all. §4.1 now states this is the intended fix in
   preference to a property test alone (a test is still recommended as a
   second line of defense, per test-writer's ownership).
4. **Two-live-stops and atomic stop replacement were not addressed
   anywhere, and the data model couldn't represent the failure if it
   happened.** A single `positions.stop_order_id` scalar can't reveal
   that two stop orders are live at once if replacement isn't sequenced
   correctly. **[PARTIALLY FIXED]** — `execution/orders.py` gained
   `replace_stop()` with explicit cancel-then-confirm-then-place
   sequencing that refuses to place a new stop unless the old one's
   cancellation is confirmed (§3.2); `stop_replacements` and
   `stop_confirmations` tables (§5) record every attempt and every
   session-open check. The exact Alpaca-specific request sequencing
   (which API calls, in which order, with what retry behavior on an
   ambiguous broker response) is implementation detail, not an
   architecture decision — tracked in §12.
5. **`OrderResult` didn't carry the broker's raw rejection payload —
   inconsistent with this document's own "every zero gets its raw
   response" pattern elsewhere.** A rejected or malformed stop is exactly
   the failure that leaves a position unprotected, and diagnosing it
   without the raw response requires log-diving, which BUILD-BRIEF's
   dashboard requirement exists to prevent. **[FIXED]** — `OrderResult`
   (§3.1) now carries `raw_response: dict`, always populated, matching
   `raw_events_errors` and `research_calls`' existing pattern.
6. **The kill-switch failure mode on a broker/data outage was
   unspecified — does it fail open or fail closed?** A check that
   silently proceeds as "not tripped" on a data failure is a way to lose
   exactly when things are already going wrong. **[FIXED]** —
   `kill_switches.check()` (§3.2) now explicitly fails closed:
   inability to build a reliable `PortfolioState` returns
   `tripped=True, reason="portfolio_state_unreliable"`, recorded in
   `kill_switch_events` (§5) exactly like any other trip.
7. **No storage table or defined `BacktestResult` shape existed for
   backtest outputs, even though every other pipeline stage got one** —
   and the dashboard's annual-hurdle-vs-S&P/T-bills requirement depends
   on it. **[FIXED]** — `BacktestResult`/`BacktestSampleStats` (§3.1) are
   now fully defined, including `return_per_trade_needed_to_break_even`
   explicitly as a field, and `backtest_results`/`backtest_sample_stats`
   tables exist in §5.
8. **Minor completeness gaps**: `governor_profit_share` was described as
   adaptive but wasn't in `ADAPTIVE_PARAMETERS` and so wasn't covered by
   any of §6.3's safeguards; `EvidenceSample` and `evidence_strength`
   were referenced but never defined. **[FIXED]** — `governor_profit_share`
   is now in `ADAPTIVE_PARAMETERS` with its own `MIN_SAMPLE_SIZE` entry
   (§6.1); `EvidenceSample` is fully defined in §3.1, including
   `evidence_strength` as a scalar derived only from `effect_size`,
   `significance`, and sample count — never from a model's stated
   confidence or anything in `ResearchView`.

**Confirmed correct, not findings** (risk-reviewer's own words, quoted
because CLAUDE.md house rule 1 asks reviewers to say so, not just flag
problems): hard-bounds physical isolation (§6.1–6.2, modulo the standard
caveat that Python `frozen=True` is bypassable via `object.__setattr__`
by a determined future edit — worth a code-review note, not a design
flaw); evidence intake restricted to closed/scored outcomes with no
constructor path from unrealized P&L or model confidence; the 3:1
tighten/loosen asymmetry hard-coded as a constant the system cannot
adjust about itself; bounded step size with full evidence logging and
auto-reversion; `MIN_SAMPLE_SIZE` values honestly labeled provisional
rather than dressed up as validated; event-sourced append-only storage as
the right choice for after-the-fact reconstruction; and the cost
governor's local-ledger-plus-reconciliation design as correctly answering
TRAPS.md's specific traps.

**Recommendation, followed:** none of these findings blocked merging this
document, and none required an architectural rethink — all eight were
closeable by tightening a signature, adding a field, or adding a table,
which is what the edits above do. Per risk-reviewer's own closing
instruction, this review must run again, and should be treated as
blocking rather than optional, once `risk/sizing.py`,
`risk/kill_switches.py`, and `risk/adaptive_params.py` have actual
bodies — CLAUDE.md house rule 5 already requires human review of that
code regardless.

---

## 11. Cost-auditor's findings on the cost governor design

`cost-auditor` (read-only tools, model `sonnet`) independently reviewed
§7 (and the cost-related parts of §3/§5) against its own checklist.
Headline verdict from the review: the design got the *shape* of every
fix right, but several of the claims §7 made about itself weren't yet
backed by a type or a signature — `CostEstimate`, `CostEvent`,
`GovernorDecision`, and `CostContext` were referenced throughout §3.2 but
never actually defined, which is exactly the asymmetry §4.1 avoided for
the Claude/code boundary by giving `ResearchView` no field that could
hold a size. Findings, condensed, each marked with what this document
now does about it:

1. **Cache tokens were priced correctly in prose but not enforced by any
   type.** No pricing function signature existed to check that an
   implementation actually reads `cache_read_input_tokens` and
   `cache_creation_input_tokens` rather than pricing from
   `input_tokens`/`output_tokens` alone — TRAPS.md's own estimate is that
   this understates the bill by about half. **[FIXED]** — `UsageComponents`
   (§3.1) now carries all five usage fields as named fields, and
   `cost.tracker.price()` (§3.2) is documented as the only function
   permitted to convert a usage object into a dollar figure, with an
   explicit test requirement for test-writer (a fixture asserting the
   priced total changes when each field changes independently).
2. **Raw usage object stored verbatim — verified correct**, present
   redundantly in both `research_call_turns.usage_raw` and
   `cost_events.raw_usage_json` (§5). No change needed.
3. **Cost API cents-parsing trap was never restated at the one place it
   actually bites — reconciliation.** The rest of §7 named every TRAPS.md
   item explicitly except this one. **[FIXED]** — `reconcile_day()`'s
   documentation (§3.2, §7.1) now explicitly states the Cost API's
   amount is parsed as a decimal-string count of cents before comparison.
4. **Whole-days-only handling was correctly designed for gating, but
   reconciliation was described as passive (dashboard-flag-only) and
   total-only (not broken out by `kind`), which could let a scheduled
   mispricing and a manual mispricing cancel out in aggregate.**
   **[FIXED]** — `reconcile_day()` now reconciles per exact
   `(date, kind, component)` triple (§7.1, §5's `cost_reconciliation_events`
   table) and pauses new scheduled authorizations on a threshold breach
   until a human acknowledges it, rather than only flagging the dashboard.
5. **Cost-API pagination/page-limit trap was entirely unaddressed** —
   every other TRAPS.md item in §7 was named explicitly; this one wasn't.
   **[FIXED]** — `reconcile_day()` is now specified to query exactly one
   closed day at a time by construction, with an explicit requirement
   that any future multi-day backfill path take its own page-size
   parameter rather than relying on the API default (§3.2, §7.1).
6. **Web search cost was named in the pricing rule, but its capture
   mechanism had an unresolved contradiction with §4.2's forced
   `tool_choice`**: a single forced tool choice from the start of the
   call cannot also let Claude call `web_search`, yet `tools_offered` and
   the module's stated job implied a research process that does search —
   and `ResearchCallLog.usage_raw` was typed as one `dict`, which could
   only hold one API response's usage even though a real research flow
   plausibly needs several. **[FIXED]** — `investigate()` is now
   explicitly multi-turn: zero or more exploration turns
   (`tool_choice: "auto"`, `web_search` available) followed by exactly
   one forced-choice extraction turn (§4.2); `ResearchCallLog.api_turns`
   (§3.1) is a list, and `cost_cents` is the sum of `price()` over every
   turn, not just the last one (§7.3).
7. **Scheduled-vs-manual separation was a real, verified mechanism, but
   manual spend had no cap at all** — and §9.6's judgement-mode backtest
   explicitly routes through the manual bucket, which could otherwise run
   past any dollar figure. **[FIXED]** — `authorize()` (§7.2) now checks
   `kind="manual"` against a separate, human-set, non-adaptive
   `MANUAL_SPEND_CAP_CENTS_PER_MONTH` constant; "separated" no longer
   means "unbounded."
8. **Annualizing-refused design correctly verified** — `cost/ledger.py`
   exposing no annualizing function is about as strong a guarantee as is
   available pre-implementation. The specific minimum window was
   correctly left open, but wasn't listed in §11 (now §12)'s open items
   the way `MIN_SAMPLE_SIZE` was. **[FIXED]** — now explicitly cross-
   referenced in §7.4 and added to §12.

**Additional findings beyond the 8-point checklist, both addressed:**
the realized-profit-contribution formula summed only per-trade winners
(floored at zero *per trade*), which lets a net-losing month still grow
the cap if it contained any winner at all — **[FIXED]**, §7.2 now nets
realized P&L for the month as a whole before flooring at zero, matching
a plain reading of BUILD-BRIEF's "a fraction of *realised* profit."
`governor_profit_share` sat outside every adaptive-parameter safeguard —
**[FIXED]**, folded into `ADAPTIVE_PARAMETERS` (§6.1), shared with
risk-reviewer's finding 8 above.

**Answer to "is `authorize()` positioned early enough?"** — yes for call-
site coverage: `investigate()` is the only place in the whole
architecture that can spend money on Claude, reused by
`backtest/judgement.py` rather than a parallel implementation (§7.3), so
there is no code path that bypasses the gate outright. The gate could
not stop an in-flight call from overrunning a *pre-call* estimate — it
can only affect the next call — which is an inherent limit of estimating
before the fact, not a design defect; TRAPS.md's own $10/1000-query rule
is exactly what makes the post-call `record()` reconciliation this
document specifies necessary as a second check, not a redundant one.

---

## 12. Open items deferred to implementation

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
- Minimum window (sample size) before the dashboard computes an
  annualized cost hurdle at all — `cost-auditor`, §7.4.
- `MANUAL_SPEND_CAP_CENTS_PER_MONTH`'s value — a human-set constant;
  proposed by whoever owns the judgement-mode backtest usage pattern
  that will consume it, against real observed API costs.
- Exact Alpaca request sequencing inside `replace_stop()` (which calls,
  what retry behavior on an ambiguous cancel/replace response) —
  `execution` owner (human-review-required), informed by
  `market-structure`'s read on how Alpaca actually behaves at the
  daily stop re-arm boundary.
- Statistical methodology behind `EvidenceSample.effect_size` /
  `.significance` / `.evidence_strength` (§3.1) — `strategy-analyst` +
  `backtest-engineer`, since this is the same power-analysis work that
  will eventually validate or revise the `MIN_SAMPLE_SIZE` placeholders.
