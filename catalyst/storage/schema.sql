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
    decision_id     TEXT NOT NULL REFERENCES risk_decisions(id),
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
    kind           TEXT NOT NULL CHECK (kind IN ('scheduled','manual')),
    component      TEXT NOT NULL,
    priced_cents   TEXT NOT NULL,
    priced_at      TEXT NOT NULL,
    api_call_id    TEXT
);

CREATE TABLE IF NOT EXISTS cost_governor_events (
    cycle_id       TEXT,
    requested_kind TEXT NOT NULL CHECK (requested_kind IN ('scheduled','manual')),
    estimate_cents TEXT NOT NULL,
    cap_cents      TEXT NOT NULL,
    decision       TEXT NOT NULL,
    reason         TEXT,
    at             TEXT NOT NULL
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
    action_taken         TEXT NOT NULL,
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
