-- Catalyst schema. Event-sourced and append-only for anything
-- decision-related (ARCHITECTURE.md section 5): only positions and
-- fills mutate; research_calls and risk_decisions never do.
-- Changes to this file route through a single coordinating session.

-- data/ layer
CREATE TABLE IF NOT EXISTS raw_events (
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    payload_raw   TEXT NOT NULL,          -- verbatim upstream JSON
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS raw_events_errors (
    source        TEXT NOT NULL,
    attempted_at  TEXT NOT NULL,
    error_text    TEXT NOT NULL           -- the raw response beside the zero
);

-- discovery/ layer
CREATE TABLE IF NOT EXISTS candidates (
    id                        TEXT PRIMARY KEY,
    ticker                    TEXT NOT NULL,
    catalyst_type             TEXT NOT NULL,
    catalyst_date             TEXT NOT NULL,
    catalyst_date_confidence  TEXT NOT NULL CHECK (catalyst_date_confidence IN ('confirmed','estimated')),
    source_event_ids          TEXT NOT NULL,   -- JSON array
    discovered_at             TEXT NOT NULL,
    sector                    TEXT NOT NULL,
    correlation_tags          TEXT NOT NULL    -- JSON array
);

-- WHERE A CANDIDATE CAME FROM, and why it was nominated.
--
-- A SIDE TABLE, not a tenth column on `candidates`. That table is
-- written with positional INSERTs in a great many places and adding a
-- column silently shifts every one of them - learned by doing it the
-- other way first and breaking 157 tests at once.
--
-- Needed because two different things now produce candidates: the
-- mechanical screen (Form 4 clusters and cross-feed conjunctions, which
-- is line-for-line the backtested arm) and Claude's own hunt over the
-- raw feed. They must be TELLABLE APART for the rest of the bot's life,
-- or the backtest's measured edge silently stops describing what is
-- running, and nobody can answer "are the model's own picks any good?"
-- with anything but an opinion.
--
-- `rationale` is the model's two-sentence reason for nominating. It is
-- audit trail only: no arithmetic reads it, and it is NOT the trade
-- thesis, which a full research pass writes afterwards.
CREATE TABLE IF NOT EXISTS candidate_origin (
    candidate_id  TEXT PRIMARY KEY REFERENCES candidates(id),
    origin        TEXT NOT NULL,     -- 'screen' | 'hunt'
    rationale     TEXT,
    nominated_at  TEXT NOT NULL
);

-- research/ layer
CREATE TABLE IF NOT EXISTS research_calls (
    id              TEXT PRIMARY KEY,
    candidate_id    TEXT NOT NULL REFERENCES candidates(id),
    model           TEXT NOT NULL,
    prompt_rendered TEXT NOT NULL,
    tools_offered   TEXT NOT NULL,          -- JSON array
    cost_cents      TEXT NOT NULL,          -- Decimal as string
    latency_ms      INTEGER NOT NULL,
    skipped_reason  TEXT,
    called_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_call_turns (
    call_id      TEXT NOT NULL REFERENCES research_calls(id),
    turn_index   INTEGER NOT NULL,
    raw_response TEXT NOT NULL,             -- verbatim API response
    usage_raw    TEXT NOT NULL,             -- verbatim usage object (TRAPS.md)
    stop_reason  TEXT NOT NULL,
    PRIMARY KEY (call_id, turn_index)
);

CREATE TABLE IF NOT EXISTS research_views (
    candidate_id           TEXT PRIMARY KEY REFERENCES candidates(id),
    direction              TEXT NOT NULL CHECK (direction IN ('long','short','no_trade')),
    conviction             REAL NOT NULL,
    thesis                 TEXT NOT NULL,
    invalidation           TEXT NOT NULL,
    expected_holding_days  INTEGER NOT NULL,
    priced_in              INTEGER NOT NULL,
    priced_in_reasoning    TEXT NOT NULL
);

-- risk/ layer
CREATE TABLE IF NOT EXISTS risk_decisions (
    id                       TEXT PRIMARY KEY,
    candidate_id             TEXT NOT NULL REFERENCES candidates(id),
    action                   TEXT NOT NULL CHECK (action IN ('trade','skip')),
    side                     TEXT CHECK (side IN ('long','short')),
    notional_usd             TEXT,
    qty                      TEXT,
    stop_price               TEXT,
    planned_exit_date        TEXT,
    skip_reasons             TEXT NOT NULL,  -- JSON array
    adaptive_params_snapshot TEXT NOT NULL,  -- JSON object
    decided_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS limit_applications (
    decision_id     TEXT NOT NULL REFERENCES risk_decisions(id),
    rule_name       TEXT NOT NULL,
    bound_value     TEXT NOT NULL,
    requested_value TEXT NOT NULL,
    bound_type      TEXT NOT NULL CHECK (bound_type IN ('hard','adaptive')),
    binding         INTEGER NOT NULL
);

-- WHY A BOUND LANDED WHERE IT DID, in a sentence.
--
-- Its own table rather than a seventh column on limit_applications,
-- for the same reason entry_market_context is its own table: that one
-- is written with positional INSERTs in a great many places, and a new
-- column silently shifts every one of them. (Learned by doing it the
-- other way first and breaking 157 tests at once.)
--
-- Needed at all because a bound derived from the stock's own measured
-- history no longer explains itself: "per_stock_stop_width 0.08 vs
-- 0.50" is not an answer to "why is this position that size".
CREATE TABLE IF NOT EXISTS limit_application_notes (
    decision_id TEXT NOT NULL REFERENCES risk_decisions(id),
    rule_name   TEXT NOT NULL,
    note        TEXT NOT NULL,
    PRIMARY KEY (decision_id, rule_name)
);

CREATE TABLE IF NOT EXISTS refusals (
    decision_id      TEXT NOT NULL REFERENCES risk_decisions(id),
    candidate_id     TEXT NOT NULL REFERENCES candidates(id),
    price_at_refusal TEXT NOT NULL,
    refused_at       TEXT NOT NULL,
    scored_at        TEXT,
    outcome_price    TEXT,
    outcome_return   TEXT
);

CREATE TABLE IF NOT EXISTS kill_switch_events (
    triggered_at             TEXT NOT NULL,
    switch_name              TEXT NOT NULL,
    portfolio_state_snapshot TEXT NOT NULL,
    cleared_at               TEXT
);

CREATE TABLE IF NOT EXISTS adaptive_param_log (
    parameter             TEXT NOT NULL,
    old_value             TEXT NOT NULL,
    new_value             TEXT NOT NULL,
    sample_ids            TEXT NOT NULL,   -- JSON array
    evidence_window_start TEXT NOT NULL,
    evidence_window_end   TEXT NOT NULL,
    evidence_summary      TEXT NOT NULL,
    changed_at            TEXT NOT NULL,
    reverses_to           TEXT NOT NULL,
    reverted_at           TEXT
);

-- execution/ layer
CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    -- Holds the CANDIDATE id, which is what execution/ writes and what
    -- reconcile joins on (risk_decisions.candidate_id). It referenced
    -- risk_decisions(id) - a uuid nothing ever puts here - so under
    -- production's PRAGMA foreign_keys=ON every entry order INSERT
    -- failed AFTER the order had already been sent to the broker
    -- (stress-tester defect 1; tests/test_stress_stage5.py).
    decision_id     TEXT NOT NULL REFERENCES candidates(id),
    broker_order_id TEXT,
    side            TEXT NOT NULL,
    qty             TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    time_in_force   TEXT NOT NULL,
    submitted_at    TEXT NOT NULL,
    status          TEXT NOT NULL,
    raw_response    TEXT NOT NULL          -- broker's verbatim response, even on rejection
);

CREATE TABLE IF NOT EXISTS stop_replacements (
    position_id       TEXT NOT NULL,
    old_stop_order_id TEXT,
    new_stop_order_id TEXT,
    status            TEXT NOT NULL CHECK (status IN ('replaced','failed_cancel_unconfirmed')),
    raw_response      TEXT NOT NULL,
    replaced_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stop_confirmations (
    position_id         TEXT NOT NULL,
    checked_at          TEXT NOT NULL,
    live_stop_order_ids TEXT NOT NULL,     -- JSON array, queried fresh from broker
    status              TEXT NOT NULL CHECK (status IN ('ok','unprotected','duplicate_stops'))
);

CREATE TABLE IF NOT EXISTS fills (
    order_id              TEXT NOT NULL REFERENCES orders(id),
    price                 TEXT NOT NULL,
    qty                   TEXT NOT NULL,
    filled_at             TEXT NOT NULL,
    broker_reported_price TEXT NOT NULL,
    modeled_slippage      TEXT
);

-- WHAT THE BOOK LOOKED LIKE WHEN THE ENTRY WAS SENT.
--
-- TRAPS.md: "Paper fills pay no spread. Model the cost, but record it
-- BESIDE the broker's price, not instead of it - reconciliation
-- compares against the real fill." A paper account fills at the mid and
-- charges nothing to cross the spread, so paper P&L is optimistic by
-- roughly the half-spread on entry and again on exit. On the small caps
-- where the insider-cluster edge lives that is tens of basis points a
-- side, which is the difference between beating the S&P and only
-- appearing to.
--
-- Its own table rather than a column on `orders`, because it is market
-- context rather than order data, and because `orders` is written with
-- positional INSERTs in a great many places that a new column would
-- silently shift.
CREATE TABLE IF NOT EXISTS entry_market_context (
    order_id       TEXT PRIMARY KEY REFERENCES orders(id),
    half_spread_bp TEXT NOT NULL,       -- measured from live NBBO
    last_close     TEXT,
    recorded_at    TEXT NOT NULL
);

-- THE SECOND OPINION ON THE ONE NUMBER EVERYTHING DESCENDS FROM.
--
-- Every traded figure - qty, stop, exposure - is derived from a single
-- live Alpaca quote. `data/quote_check` compares that quote against the
-- newest cached daily close before the risk engine runs: a deviation no
-- market produces is the shape of a decimal error, a wrong symbol or an
-- unadjusted corporate action, and it stops the candidate.
--
-- It is recorded here rather than only in the cycle report because a
-- FLAGGED quote (large, but real) is passed through deliberately, and a
-- pass-through that exists only in a process that has since exited is
-- indistinguishable from never having looked. One row per candidate;
-- the primary key keeps a candidate re-checked every cycle to a single
-- current row rather than an unbounded log.
CREATE TABLE IF NOT EXISTS quote_cross_checks (
    candidate_id    TEXT PRIMARY KEY REFERENCES candidates(id),
    ticker          TEXT NOT NULL,
    live_price      TEXT NOT NULL,
    reference_close TEXT,               -- NULL when there was no history
    reference_day   TEXT,
    deviation       TEXT,               -- signed fraction, e.g. "-0.9010"
    checked         INTEGER NOT NULL,   -- 0 = no history, NOT "passed"
    flagged         INTEGER NOT NULL,
    refused         INTEGER NOT NULL,
    note            TEXT NOT NULL,      -- the sentence shown to the owner
    checked_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id                TEXT PRIMARY KEY,
    ticker            TEXT NOT NULL,
    entry_order_ids   TEXT NOT NULL,       -- JSON array
    stop_order_id     TEXT,
    opened_at         TEXT NOT NULL,
    planned_exit_date TEXT NOT NULL,
    status            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS closed_trades (
    position_id           TEXT PRIMARY KEY REFERENCES positions(id),
    account_mode          TEXT NOT NULL DEFAULT 'paper' CHECK (account_mode IN ('paper','live')),
    entry_price           TEXT NOT NULL,
    exit_price            TEXT NOT NULL,
    exit_reason           TEXT NOT NULL,
    realized_pnl_cents    INTEGER NOT NULL,
    expected_holding_days INTEGER NOT NULL,
    actual_holding_days   INTEGER NOT NULL,
    closed_at             TEXT NOT NULL
);

-- cost/ layer
CREATE TABLE IF NOT EXISTS cost_events (
    id             TEXT PRIMARY KEY,
    raw_usage_json TEXT NOT NULL,          -- verbatim (TRAPS.md)
    model          TEXT NOT NULL,          -- required for repricing (audit F3)
    kind           TEXT NOT NULL CHECK (kind IN ('scheduled','manual')),
    component      TEXT NOT NULL,
    priced_cents   TEXT,                   -- NULL = recorded but NOT priced
                                           -- (unknown model; audit F2) -
                                           -- governor blocks while any
                                           -- unpriced row exists
    priced_at      TEXT NOT NULL,
    api_call_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_cost_events_kind_day
    ON cost_events (kind, component, priced_at);

CREATE TABLE IF NOT EXISTS cost_governor_events (
    cycle_id       TEXT,
    requested_kind TEXT NOT NULL CHECK (requested_kind IN ('scheduled','manual')),
    estimate_cents TEXT NOT NULL,
    cap_cents      TEXT NOT NULL,
    decision       TEXT NOT NULL,
    reason         TEXT,
    at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_reprice_events (
    id            TEXT PRIMARY KEY,
    cost_event_id TEXT NOT NULL REFERENCES cost_events(id),
    old_cents     TEXT,                  -- NULL = was unpriced
    new_cents     TEXT NOT NULL,
    repriced_at   TEXT NOT NULL          -- every adjustment logged (CLAUDE.md)
);

CREATE TABLE IF NOT EXISTS cost_reconciliation_events (
    id                   TEXT PRIMARY KEY,
    target_date          TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    component            TEXT NOT NULL,
    local_total_cents    TEXT NOT NULL,
    cost_api_total_cents TEXT NOT NULL,
    discrepancy_cents    TEXT NOT NULL,
    threshold_cents      TEXT NOT NULL,
    api_raw_response     TEXT NOT NULL,    -- verbatim payload beside the zero (house rule 3)
    api_record_count     INTEGER NOT NULL,
    action_taken         TEXT NOT NULL,
    -- WHICH TEST FIRED. Owner-reported 2026-08-20: seven consecutive
    -- rows reading "scheduled_paused", three of them with a $0.00
    -- discrepancy, and nothing anywhere saying why. A pause that halts
    -- trading is the last thing on this dashboard that should be
    -- unexplained (house rule 3).
    pause_reason         TEXT,
    -- The ACCUMULATED drift behind a pause, as distinct from the day's
    -- own discrepancy. A drift-caused pause has a SMALL day figure by
    -- definition, so re-judging it against that figure always cleared
    -- it and the next cycle paused again on the same drift.
    drift_cents          TEXT,
    acknowledged_by      TEXT,
    acknowledged_at      TEXT,
    reconciled_at        TEXT NOT NULL
);

-- backtest/ layer
CREATE TABLE IF NOT EXISTS backtest_results (
    id                  TEXT PRIMARY KEY,
    strategy_name       TEXT NOT NULL,
    mode                TEXT NOT NULL CHECK (mode IN ('structural','judgement')),
    date_range_start    TEXT NOT NULL,
    date_range_end      TEXT NOT NULL,
    spy_total_return    TEXT NOT NULL,     -- the benchmark, on every run
    strategy_return_net TEXT NOT NULL,
    excess_return_net   TEXT NOT NULL,
    costs_applied       TEXT NOT NULL,     -- JSON object
    market_regime_notes TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_sample_stats (
    result_id                             TEXT NOT NULL REFERENCES backtest_results(id),
    sample_kind                           TEXT NOT NULL CHECK (sample_kind IN ('in_sample','out_of_sample')),
    sample_size                           INTEGER NOT NULL,
    hit_rate                              TEXT NOT NULL,
    mean_return                           TEXT NOT NULL,
    median_return                         TEXT NOT NULL,
    worst_single_outcome                  TEXT NOT NULL,
    max_drawdown                          TEXT NOT NULL,
    return_per_trade_needed_to_break_even TEXT NOT NULL
);

-- observability (requested by ui-designer, folded in by the
-- coordinating session; DDL authored in catalyst/dashboard/schema_logs.sql)
CREATE TABLE IF NOT EXISTS logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,          -- ISO-8601 UTC
    level          TEXT NOT NULL CHECK (level IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')),
    component      TEXT NOT NULL,          -- 'data.edgar', 'risk.evaluate', ...
    message        TEXT NOT NULL,
    cycle_id       TEXT,                   -- ties a line to one orchestrator cycle
    candidate_id   TEXT,                   -- ties a line to one decision trace
    traceback_text TEXT,                   -- full traceback on errors
    context_json   TEXT                    -- state at the time, JSON object
);
CREATE INDEX IF NOT EXISTS idx_logs_ts        ON logs (ts);
CREATE INDEX IF NOT EXISTS idx_logs_level_ts  ON logs (level, ts);
CREATE INDEX IF NOT EXISTS idx_logs_component ON logs (component, ts);

-- daily equity marks so performance-vs-SPY can be drawn from real state,
-- not reconstructed from closed trades alone (ui-designer request #1:
-- without this, unrealised P&L is invisible and exposure matching is
-- impossible). Written once per cycle from the confirmed broker read.
CREATE TABLE IF NOT EXISTS equity_snapshots (
    day                  TEXT NOT NULL,    -- ISO date, UTC
    taken_at             TEXT NOT NULL,
    equity_usd           TEXT NOT NULL,
    settled_cash_usd     TEXT NOT NULL,
    positions_notional   TEXT NOT NULL,
    source               TEXT NOT NULL,    -- 'broker_read'
    PRIMARY KEY (day, source)
);

-- Owner-entered token prices, DATE-EFFECTIVE. Published rates change
-- (Sonnet 5's introductory pricing ends 2026-08-31), and the alternative
-- to this table was editing pricing.py and redeploying.
--
-- Effective-FROM, never retroactive: a row priced last month keeps the
-- rate that was in force when the tokens were actually bought. Rewriting
-- history would make the nightly comparison against the real Anthropic
-- bill drift for reasons nobody could reconstruct.
--
-- Append-only by convention: a correction is a NEW row, so the record of
-- what was believed when is never lost.
CREATE TABLE IF NOT EXISTS pricing_overrides (
    id                    TEXT PRIMARY KEY,
    model                 TEXT NOT NULL,
    effective_from        TEXT NOT NULL,   -- ISO date, inclusive
    input_cents_per_mtok  TEXT NOT NULL,   -- decimal string, cents
    output_cents_per_mtok TEXT NOT NULL,
    set_by                TEXT NOT NULL,
    set_at                TEXT NOT NULL,
    note                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_pricing_overrides_lookup
    ON pricing_overrides (model, effective_from DESC);

-- Periodic re-check of an OPEN position against fresh news. A review can
-- only ever bring an exit date FORWARD (research/position_review.py), so
-- these rows explain early exits and, just as importantly, record the
-- times the model was asked and said the thesis was intact.
CREATE TABLE IF NOT EXISTS position_reviews (
    id                     TEXT PRIMARY KEY,
    position_id            TEXT NOT NULL REFERENCES positions(id),
    ticker                 TEXT NOT NULL,
    action                 TEXT NOT NULL
                           CHECK (action IN ('hold','exit_now','no_opinion')),
    invalidation_triggered INTEGER NOT NULL,
    reasoning              TEXT NOT NULL,
    what_changed_json      TEXT,
    prompt_rendered        TEXT,      -- what the model saw (BUILD-BRIEF)
    raw_response_json      TEXT,      -- verbatim, never named fields only
    model                  TEXT,
    cost_cents             TEXT,
    skipped_reason         TEXT,      -- set when no call was made at all
    reviewed_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_position_reviews_pos
    ON position_reviews (position_id, reviewed_at);

-- Form 4 filings already downloaded, so a pass never re-fetches one.
-- WITHOUT THIS the feed made ~2,815 requests every 15-minute cycle -
-- 9.4 minutes of continuous traffic, a 63% duty cycle - and sec.gov
-- rate-limited the bot's IP on 2026-08-11. The parsed filing is stored
-- verbatim so it can be REPLAYED into the window rather than skipped:
-- insider-cluster detection needs every purchase in the window to count
-- distinct owners, so skipping would break the strategy silently.
CREATE TABLE IF NOT EXISTS edgar_filings (
    accession   TEXT PRIMARY KEY,
    parsed_json TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edgar_filings_fetched
    ON edgar_filings (fetched_at);

-- The benchmark BASELINE: what "the same money in SPY instead" means.
--
-- START_CAPITAL_CENTS was a constant, so the whole comparison assumed
-- $1,000 forever. Change the Alpaca account and every performance
-- figure silently compares a different account against the old base.
--
-- Append-only, exactly like adaptive_param_log: the current baseline is
-- the latest row, so the audit trail and the live state cannot disagree
-- because they ARE the same rows. Every change records WHY.
CREATE TABLE IF NOT EXISTS benchmark_baselines (
    id                  TEXT PRIMARY KEY,
    capital_cents       TEXT NOT NULL,      -- what SPY is bought with
    start_date          TEXT NOT NULL,      -- the day it is bought
    source              TEXT NOT NULL CHECK (source IN
                            ('first_run', 'account_changed', 'owner_set')),
    account_fingerprint TEXT NOT NULL,      -- hash of the broker account id,
                                            -- NEVER a key or a secret
    reason              TEXT NOT NULL,
    set_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_benchmark_baselines_at
    ON benchmark_baselines (set_at DESC);

-- Every closed-day comparison of what the ledger PRICED against what
-- Anthropic actually BILLED, and what that said about the rate table.
-- Written whether or not it changed anything: the quiet "checked and
-- agreed" rows are the evidence that replaces pricing.py's 90-day
-- calendar guess with a measurement (catalyst/cost/measured_rates.py).
CREATE TABLE IF NOT EXISTS measured_rate_observations (
    id                        TEXT PRIMARY KEY,
    target_date               TEXT NOT NULL,   -- the closed day measured
    model                     TEXT NOT NULL,
    local_total_cents         TEXT NOT NULL,   -- decimal string
    billed_total_cents        TEXT NOT NULL,
    ratio                     TEXT NOT NULL,   -- billed / local
    applied                   INTEGER NOT NULL,-- 1 = the table was changed
    reason                    TEXT NOT NULL,
    old_input_cents_per_mtok  TEXT,
    new_input_cents_per_mtok  TEXT,
    old_output_cents_per_mtok TEXT,
    new_output_cents_per_mtok TEXT,
    observed_at               TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_measured_rate_observations_day
    ON measured_rate_observations (target_date DESC);

-- The pricing MULTIPLIERS (cache write/read, web search), measured from
-- the bill's own token_type breakdown rather than typed in from
-- documentation. Absent row = the documented defaults in pricing.py, so
-- an install that never measures prices exactly as it always did
-- (catalyst/cost/factors.py).
CREATE TABLE IF NOT EXISTS measured_factors (
    id               TEXT PRIMARY KEY,
    model            TEXT NOT NULL,
    effective_from   TEXT NOT NULL,   -- ISO date, inclusive
    cache_write      TEXT NOT NULL,   -- decimal string, x input rate
    cache_write_1h   TEXT NOT NULL,
    cache_read       TEXT NOT NULL,
    web_search_cents TEXT NOT NULL,   -- cents per query
    set_by           TEXT NOT NULL,
    set_at           TEXT NOT NULL,
    note             TEXT
);

CREATE INDEX IF NOT EXISTS idx_measured_factors_lookup
    ON measured_factors (model, effective_from DESC);

-- One company's SIC industry code, cached from EDGAR's submissions API.
-- A SIC changes when a company reorganises - years apart, if ever - so a
-- hit is kept indefinitely and only failures are retried. Exists so
-- insider candidates carry a real sector: without one they all key on
-- "unknown" and the correlated-cluster cap treats unrelated companies as
-- a single bet (catalyst/data/sources/edgar_company.py).
CREATE TABLE IF NOT EXISTS company_sic (
    cik        TEXT PRIMARY KEY,
    sic        TEXT NOT NULL,        -- "" = EDGAR has none for this company
    fetched_at TEXT NOT NULL,
    note       TEXT                  -- why it is empty, when it is
);

-- One closed day's Anthropic usage, split by which API key spent it.
--
-- OWNER-REPORTED 2026-08-23: "on 17th dashboard says i spent $3.64 but
-- admin console says $2.95". Neither figure was wrong arithmetic - the
-- Cost API and our own pricing of Anthropic's token counts agreed to
-- four decimal places that day. What differs is SCOPE: the Cost API
-- bills the whole organisation and cannot be filtered to one key, so a
-- console view that IS filtered is a smaller and equally true number.
--
-- The usage report is already fetched grouped by api_key_id, so the
-- answer was in the response all along and nothing kept it. This is a
-- SIDE TABLE on purpose (CLAUDE.md: never add a column to a hot table -
-- cost_events is written with positional INSERTs in many places).
--
-- Written on every backfill pass, including days needing no correction,
-- so the evidence exists for every closed day rather than only the ones
-- that disagreed. INSERT OR REPLACE keyed on the day, key and model, so
-- re-running a day restates it rather than doubling it.
CREATE TABLE IF NOT EXISTS usage_by_key (
    target_date TEXT NOT NULL,       -- the closed day, YYYY-MM-DD
    api_key_id  TEXT NOT NULL,       -- as Anthropic identifies it
    model       TEXT NOT NULL,
    cents       TEXT NOT NULL,       -- priced by price(), decimal string
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (target_date, api_key_id, model)
);

CREATE INDEX IF NOT EXISTS idx_usage_by_key_day
    ON usage_by_key (target_date DESC);

-- What the once-a-day SPY refresh actually did, kept so the dashboard
-- can tell a weekend from a broken feed.
--
-- OWNER-REPORTED 2026-08-24: "its stopped tracking SPY" - the SPY line
-- on the performance chart ending days before the bot's own line.
--
-- Both explanations look identical on the chart. Alpaca publishes no
-- daily bar on a weekend or a market holiday, so a line that stops on
-- Friday is correct and needs nothing; a refresh being refused by the
-- credentials also stops the line, forever, and needs the owner. The
-- refresh already knew which had happened and only ever said so in the
-- log, where the chart cannot reach it.
--
-- A SIDE TABLE, not a column on anything hot (CLAUDE.md). One row per
-- attempt, with the raw upstream body beside a failure (house rule 3).
CREATE TABLE IF NOT EXISTS benchmark_refreshes (
    checked_at   TEXT PRIMARY KEY,   -- UTC ISO8601
    outcome      TEXT NOT NULL,      -- 'updated', or refresh's own reason
    routine      INTEGER NOT NULL,   -- 1 = nothing wrong; 0 = needs a look
    bars_written INTEGER NOT NULL,
    last_bar_day TEXT,               -- newest SPY close after the pass
    feed         TEXT,
    raw_response TEXT                -- verbatim upstream, on a failure
);

CREATE INDEX IF NOT EXISTS idx_benchmark_refreshes_recent
    ON benchmark_refreshes (checked_at DESC);

-- When each company's XBRL companyfacts were last fetched.
--
-- Candidate A (post-earnings drift) is fully built, pre-registered and
-- graded - and had no live feed, so it produced no candidates at all.
-- On the bake-off it was the best-behaved arm out of sample: hit rate
-- 57.1% in AND out of sample, max drawdown 8.8% against the live arm's
-- 41.2%, worst trade -18.5% against -57.4%.
--
-- companyfacts is one large JSON per company and a company files
-- quarterly, so re-fetching daily would spend the SEC rate limit
-- (shared across every SEC feed in this process, TRAPS.md) on bytes
-- that cannot have changed. This table is what makes the refresh
-- incremental: a ticker is re-asked only when its cache is stale.
--
-- A SIDE TABLE, keyed by ticker, holding no candidate or order data.
CREATE TABLE IF NOT EXISTS xbrl_facts_fetched (
    ticker     TEXT PRIMARY KEY,
    cik        TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    status     TEXT NOT NULL,   -- 'ok' | 'absent' | 'failed'
    note       TEXT             -- the raw upstream reason, when not ok
);

CREATE INDEX IF NOT EXISTS idx_xbrl_facts_fetched_at
    ON xbrl_facts_fetched (fetched_at);
