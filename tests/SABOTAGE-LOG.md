# Sabotage log — negative controls for tests/test_scaffold.py

Per CLAUDE.md house rule 4 / the project's non-negotiable: every test
below was proven capable of failing before being trusted. Method for
each: sabotage a copy-restorable change in the source (never the test),
run the one test with `pytest -k <name>`, record the exact failure line,
restore the source, re-run to confirm green again.

Because `catalyst/` and `tests/` are new/untracked in this branch,
`git checkout -- <file>` cannot restore them (there is no committed
version to check out). Instead a clean baseline copy of `catalyst/` and
`tests/` was taken to the scratchpad before any sabotage began, and
every restoration below was done with `cp` from that baseline, then
verified byte-identical with `diff`. Final state: `diff -r` against the
baseline reports no differences anywhere in `catalyst/` or `tests/`.

All 19 tests pass at the end of this log (`python3 -m pytest tests/ -q`
→ `19 passed`). Baseline was 11 tests; 8 were added (see bottom section).

---

## 1. test_schema_initializes_and_has_every_architecture_table

**Broke:** deleted the `CREATE TABLE IF NOT EXISTS raw_events_errors (...)`
block from `catalyst/storage/schema.sql`.

**Failure:**
```
E       AssertionError: schema.sql is missing tables from ARCHITECTURE.md section 5: {'raw_events_errors'}
```

**Restored:** re-inserted the block; `diff` against baseline schema.sql →
identical; re-ran → pass.

---

## 2. test_research_view_structurally_cannot_carry_a_size

**Broke:** added `suggested_notional_usd: float` to `ResearchView` in
`catalyst/research/schema.py`.

**Failure:**
```
E       AssertionError: assert {'candidate_i...iced_in', ...} == {'candidate_i...iced_in', ...}
E         Extra items in the left set:
E         'suggested_notional_usd'
```

**Restored:** `cp` from baseline `catalyst/research/schema.py`; diff
identical; re-ran → pass.

---

## 3. test_research_view_tool_schema_matches_dataclass

**Broke:** removed the `"invalidation": {"type": "string"}` property
from `SUBMIT_RESEARCH_VIEW_TOOL["input_schema"]["properties"]` in
`catalyst/research/schema.py`.

**Failure:**
```
E       AssertionError: assert {'conviction'...ng', 'thesis'} == {'conviction'...asoning', ...}
E         Extra items in the right set:
E         'invalidation'
```

**Restored:** `cp` from baseline; diff identical; re-ran → pass.

---

## 4. test_sizing_signature_cannot_receive_a_research_view

**Broke:** added `view=None` parameter to `size()` in
`catalyst/risk/sizing.py`.

**Failure:**
```
E       assert 'view' not in mappingproxy(OrderedDict([('passed_gate', ...), ('view', <Parameter "view=None">)]))
```

**Restored:** `cp` from baseline; diff identical; re-ran → pass.

---

## 5. test_adaptive_params_module_has_no_writable_path_to_hard_bounds

**Broke:** added `from catalyst.risk.hard_bounds import HARD_BOUNDS` to
`catalyst/risk/adaptive_params.py`.

**Failure:**
```
E       assert 'HARD_BOUNDS' not in '"""Adaptive..."stage 5")\n'
E       'HARD_BOUNDS' is contained here:
E         ds import HARD_BOUNDS
```

**Restored:** `cp` from baseline; diff identical; re-ran → pass.

---

## 6. test_network_guard_blocks_sockets

**Broke:** commented out `socket.socket = _GuardedSocket` in
`tests/conftest.py`'s `pytest_configure`.

**Note on ambiguity, checked as instructed:** this sandbox has real
outbound network access (confirmed separately: a raw
`socket.connect(("example.com", 443))` with a 3s timeout completed with
`CONNECTED`, not a timeout or refusal). So with the guard removed, the
test's `with pytest.raises(RuntimeError, match="fully offline"): ...
s.connect(...)` block ran to completion without raising anything.

**Failure — unambiguous, caused by the guard's absence, not by a
network-layer error:**
```
>       with pytest.raises(RuntimeError, match="fully offline"):
E       Failed: DID NOT RAISE RuntimeError
```

This is the failure mode that proves the guard (not the network) is
what the test depends on: pytest reports "did not raise" rather than
any transport exception, because the real connect *succeeded*. Had this
sandbox lacked network access, the sabotaged test would instead have
failed on an uncaught `OSError`/`socket.gaierror` propagating out of the
`pytest.raises(RuntimeError, ...)` block — also an unambiguous failure,
just a different one. Either way the test cannot pass with the guard
disabled, so no test change was needed.

**Restored:** `cp` from baseline `tests/conftest.py`; diff identical;
re-ran (still passes, without network — pytest-timeout is configured
at 60s globally in pyproject.toml as a backstop against any future hang
here too) → pass.

---

## 7. test_credentials_stripped_from_test_environment

**Broke:** commented out the env-stripping loop in
`tests/conftest.py`'s `pytest_configure`, and ran pytest with
`ALPACA_TEST_VAR=x` set in the environment (a fake variable name that
merely matches the `ALPACA` prefix — no real credential name or value
was touched or printed at any point).

**Failure:**
```
E       AssertionError: credential env vars visible inside tests: ['ALPACA_TEST_VAR', 'ALPACA_SECRET_KEY', 'ANTHROPIC_BASE_URL', 'ALPACA_KEY']
```
(Only variable *names* appear in the assertion message, per the test's
own design — never values. The other three names shown are pre-existing
environment variables in this sandbox; their presence in this message
is itself evidence the strip was skipped, not a leak of any secret
value.)

**Restored:** `cp` from baseline `tests/conftest.py`; diff identical;
re-ran with `ALPACA_TEST_VAR=x` still set → pass (proving the restored
strip removes it along with everything else).

---

## Result: no test survived its sabotage

All 7 pre-existing tests, and all 8 tests added below, failed under
their respective sabotage and passed once restored. None needed to be
rewritten to be capable of failing — each caught its defect on the
first attempt.

---

## Added scaffold-level tests (with their own negative controls)

Eight tests were added to `tests/test_scaffold.py`, covering gaps judged
missing from stage 1's scaffold-level coverage: cross-layer drift
between dataclasses and `schema.sql` (only `ResearchView` had this
check before), the "safe to run twice" installer requirement from
BUILD-BRIEF.md, and three TRAPS.md-documented bug classes (float money,
dropped cache-token fields, silent annualizing) that the scaffold's
docstrings already promise not to have but nothing previously checked.

| Test | What it protects | Sabotage used | Failure produced |
|---|---|---|---|
| `test_raw_event_fields_match_raw_events_table_columns` | `RawEvent` dataclass and `raw_events` table never drift apart | added `ingest_batch_id: str = ""` to `RawEvent` | `only in dataclass={'ingest_batch_id'}` |
| `test_candidate_fields_match_candidates_table_columns` | `Candidate` dataclass and `candidates` table never drift apart | added an `exchange` column to `candidates` in schema.sql | `only in table={'exchange'}` |
| `test_schema_safe_to_apply_twice` | BUILD-BRIEF.md: installer "safe to run twice" | dropped `IF NOT EXISTS` from `CREATE TABLE positions` | `sqlite3.OperationalError: table positions already exists` |
| `test_money_shaped_dataclass_fields_are_decimal_not_float` | TRAPS.md: money must be `Decimal`, never `float` | changed `HardBounds.max_loss_per_position_pct` annotation to `float` | `HardBounds.max_loss_per_position_pct -> <class 'float'>` |
| `test_cost_governor_caps_are_decimal_not_float` | same trap, for the module-level cap constants | changed `BASE_CAP_CENTS = Decimal("500")` to `500.0` | `assert isinstance(500.0, Decimal)` → `False` |
| `test_usage_components_captures_cache_tokens_explicitly` | TRAPS.md: dropping `cache_creation_input_tokens`/`cache_read_input_tokens` understates the bill ~50% | removed `cache_creation_input_tokens` from `UsageComponents` | `UsageComponents is missing 'cache_creation_input_tokens'` |
| `test_cost_ledger_exposes_no_annualizing_function` | `cost/ledger.py`'s own contract: never annualize a partial-month figure | added an `annualized_run_rate_cents()` function to the module | `cost.ledger exposes an annualizing function: ['annualized_run_rate_cents']` |
| `test_human_review_required_files_carry_the_marker` | CLAUDE.md house rule 5: risk/execution/broker changes need human review — this marker is what makes that greppable | removed `HUMAN REVIEW REQUIRED` from `risk/sizing.py`'s docstring | `ownership marker missing from: ['risk/sizing.py']` |

Every one of these was sabotaged, run, confirmed to fail with the
message shown, restored via `cp` from the pre-sabotage backup, and
`diff`-verified byte-identical before re-confirming green.

---

## Final state

```
$ python3 -m pytest tests/ -q
...................                                                      [100%]
19 passed
```

`diff -r` of `catalyst/` and `tests/` against the pre-sabotage backup
taken in the scratchpad: no differences. `git status --short` shows only
the expected untracked new files (`catalyst/`, `tests/`, `pyproject.toml`,
`catalyst.egg-info/`) — nothing partially edited, nothing left sabotaged.

---

## Stage 2 — negative controls for tests/test_backtest.py

Per CLAUDE.md house rule 4 / the project's non-negotiable: every test
below was proven capable of failing before being trusted. Method: back
up a clean copy of `catalyst/backtest/{data,harness,scoring}.py` to the
scratchpad, sabotage the source (never the test), run the single test
with `pytest -k <name>`, record the exact failure, restore with `cp`
from the backup, `diff` byte-identical, clear `catalyst/backtest/
__pycache__` (bytecode-cache trap noted in the brief — a stale .pyc can
mask a restore), then re-run to confirm green again.

backtest-engineer negative-controlled three tests itself before handing
off (no separate note was present in this file, so the list in the
brief was trusted as-is):
- `test_t_plus_1_settlement_blocks_same_day_reuse_of_proceeds` (T+1 settlement)
- `test_point_in_time_view_cannot_see_the_future` (look-ahead guard)
- `test_fill_is_next_session_open_not_same_session_close` (fill-cost sign)

Everything else in `tests/test_backtest.py` (13 tests) was controlled
here.

### 1. `test_harness_hands_signal_fn_a_view_clamped_to_signal_day`

**Property:** the `PointInTimeView` handed to `signal_fn` is clamped to
the *signal day itself*, not some other day — a distinct property from
"the view mechanically cannot see the future" (that's test #`test_point_
in_time_view_cannot_see_the_future`, already controlled).

**Broke:** in `harness.py`, changed the view construction to peek one
session ahead: `as_of=calendar[i + 1]` instead of `as_of=day`.

**Failure:**
```
assert seen["as_of"] == days[3]
E       assert datetime.date(2020, 1, 10) == datetime.date(2020, 1, 9)
```

**Restored:** `cp` from backup; `diff` identical; re-ran → pass.

### 2. `test_forced_exit_at_end_of_range_and_no_open_positions`

**Property:** every position still open at the last session of the
range is force-closed; nothing survives the replay.

**Broke:** removed `or i == last_idx` from the exit-due condition in
`harness.py`, so a position whose planned exit is later than the range
end is never forced out.

**Failure** (the harness's own internal consistency assertion fired,
naming the exact defect):
```
assert not positions, "replay bug: positions survived the forced end-of-range exit"
E       AssertionError: replay bug: positions survived the forced end-of-range exit
```

**Restored:** `cp` from backup; `diff` identical; re-ran → pass.

### 3. `test_max_position_slots_cap`

**Property:** at most `cfg.max_positions` (5) trades can be open at
once; the 6th candidate is skipped.

**Broke:** changed `if len(positions) >= cfg.max_positions:` to `>` in
`harness.py`, permitting one extra slot.

**Failure:** the 6th candidate no longer skips on `no_free_slot` — it
is now attempted and fails on a *different* rule (equal-weight budget
divides the same equity across 6 slots instead of 5, so the 6th trade's
notional falls below the cash actually available in this fixture):
```
assert [s.reason for s in detail.skips] == ["no_free_slot"]
E       AssertionError: assert ['insufficient_settled_cash'] == ['no_free_slot']
```
Still an unambiguous failure directly caused by the sabotage (removing
it restores the original skip reason).

**Restored:** `cp` from backup; `diff` identical; re-ran → pass.

### 4. `test_benchmark_comparison_math_on_known_series` — excess_return_net subtraction order

**Property:** `excess_return_net = strategy_net - spy` (never the other
way round — this is the sign that decides whether "beating SPY" reads
as positive or negative).

**Broke:** in `scoring.py`'s `benchmark_comparison`, swapped
`excess_return_net=strategy_total_return_net - spy` to
`excess_return_net=spy - strategy_total_return_net`.

**Failure:**
```
assert b.excess_return_net == expected_net - b.spy_total_return
E       AssertionError: assert Decimal('0.107...') == (Decimal('-0.007...') - Decimal('0.1'))
```

**Restored:** `cp` from backup; `diff` identical; re-ran → pass.

### 5. `test_benchmark_comparison_uses_first_open_not_first_close`

**Property:** the SPY benchmark buys at the FIRST SESSION'S OPEN, never
its close — using the close would shrink (flatter) the benchmark and
make every strategy look relatively better.

**Broke:** in `scoring.py`'s `benchmark_total_return`, changed
`last.close / first.open - 1` to `last.close / first.close - 1`.

**Failure:**
```
assert cmp_.spy_total_return == D("110") / D("100") - 1  # open 100, not close 105
E       AssertionError: assert Decimal('0.0476...') == ((Decimal('110') / Decimal('100')) - 1)
```
Confirmed this sabotage is caught ONLY by this test, not by
`test_benchmark_comparison_math_on_known_series` (whose fixture happens
to have first open == first close, so it cannot discriminate this bug)
— which is exactly why both tests earn their keep.

**Restored:** `cp` from backup; `diff` identical; re-ran → pass.

### 6. `test_costs_strictly_reduce_reported_returns` — cost sign on exit

**Property:** costs strictly reduce reported returns; the exit haircut
must be `open*(1-cost)`, never `open*(1+cost)`.

**Broke:** in `harness.py`'s `close_position` call for the planned-exit
path, changed `bar.open * (1 - cost)` to `bar.open * (1 + cost)`.

**Failure:** with entry paying `open*(1+cost)` and exit now ALSO paying
`open*(1+cost)`, the two `(1+cost)` factors cancel exactly in
`ret = exit/entry - 1`, so cost stops reducing the return at all:
```
assert with_spread.trades[0].ret < free.trades[0].ret
E       AssertionError: assert Decimal('0.0485...') < Decimal('0.0485...')
```

**Restored:** `cp` from backup; `diff` identical; re-ran → pass.

### 7. `test_in_out_of_sample_split_is_chronological` — **test was strengthened, not just controlled**

**Property:** the in/out-of-sample split is chronological
(`entry_day <= split_date` → in-sample; `> split_date` → out-of-sample),
never randomised or reversed.

**First attempt — sabotage the property, test did NOT catch it:**
swapped the two comparisons in `harness.py`
(`in_trades = entry_day > split`, `out_trades = entry_day <= split` —
i.e. exactly reversed). Ran the existing test: **it passed.** The
fixture has 2 "early" and 2 "late" candidates on a flat-price ("AAA"
constant 10) instrument, so (a) `sample_size` stayed 2/2 either way
round, and (b) the `max(in_days) <= split < min(out_days)` check is
computed straight from `detail.trades` filtered by candidate-name
prefix — it never actually reads which bucket the harness put each
trade into, so it can't detect a swap either. **The test as written
could not fail for the property it claims to protect** — a real
static-analysis-style gap: a passing assertion that silently checks
something adjacent to, not the thing itself.

**Fix, per house rule 4 ("a test that cannot fail is not a test"):**
changed the fixture's price series from flat (`10`) to a straight
linear ramp (`10 + 0.5*i`, not %-per-session compounding, which would
make every trade's % return identical regardless of entry timing).
Added assertions that `r.in_sample.mean_return` and
`r.out_of_sample.mean_return` equal the independently-computed mean of
the `early*`/`late*` trades' own `.ret` values (read from
`detail.trades`, which is unaffected by the split bucketing under
test) — plus a guard that early and late returns are actually
different, or the test can't discriminate a swap in the first place.

**Re-verified the fix passes on correct code**, then **re-applied the
exact same swap sabotage** and confirmed it now fails:
```
assert r.in_sample.mean_return == expected_in_mean, (...)
E       AssertionError: in_sample must be built from the chronologically EARLY trades, not got mean_return=0.058839170911152452906318272, expected the early trades' mean 0.1135957233564944936418460765
```

**Restored** `harness.py` from backup (`diff` identical), re-ran →
pass. `tests/test_backtest.py` keeps the strengthened test (this is
the one intentional change to that file from this pass).

### 8 & 9. Survivorship statement dropped from `market_regime_notes`

**Property:** `SURVIVORSHIP_STATEMENT` is embedded in every result's
`market_regime_notes`, not just documentation — this is what makes it
show up on the dashboard and in the persisted row, per BUILD-BRIEF.md's
"every number says where it came from" requirement.

**Broke:** in `harness.py`'s `describe_regime`, removed
`+ SURVIVORSHIP_STATEMENT` from the returned string.

**Failure — two tests caught it, both legitimately:**

`test_every_result_carries_the_survivorship_statement`:
```
assert SURVIVORSHIP_STATEMENT in result.market_regime_notes
E       AssertionError: assert 'SURVIVORSHIP BIAS: ...' in 'Period 2020-01-06 to 2020-01-10: ... would not provide.'
```

`test_persist_result_writes_both_tables`:
```
assert "SURVIVORSHIP" in row[4], "bias statement must be persisted, not doc-only"
E       AssertionError: bias statement must be persisted, not doc-only
```

**Restored:** `cp` from backup; `diff` identical; re-ran both → pass.

### 10. `test_persist_result_writes_both_tables` — round-trip column integrity

**Property:** `persist_result` writes `spy_total_return`,
`strategy_return_net`, `excess_return_net` into the columns of the
same name — a round trip, not merely "some numbers land somewhere".

**Broke:** in `scoring.py`'s `persist_result`, reordered the three bound
values (`excess_return_net`, `strategy_total_return_net`,
`spy_total_return`) while leaving the column list unchanged, so
`spy_total_return` silently receives the excess-return value.

**Failure:**
```
assert row[2] == str(result.benchmark.spy_total_return)
E       AssertionError: assert '-0.005330...' == '0'
```

**Restored:** `cp` from backup; `diff` identical; re-ran → pass.

### 11. `test_persist_rejects_unknown_mode`

**Property:** `persist_result` refuses an unrecognised `mode` value
before writing anything.

**Broke:** removed the `if mode not in ("structural", "judgement"):
raise ValueError(...)` guard from `scoring.py`.

**Failure:** the write is no longer rejected in Python, but the
database schema itself carries a `CHECK` constraint on the `mode`
column, so it still fails — just with a different, and arguably more
honest, exception type than the test expects:
```
with pytest.raises(ValueError, match="mode"):
>           persist_result(tmp_db, result, mode="vibes")
E       sqlite3.IntegrityError: CHECK constraint failed: mode IN ('structural','judgement')
```
This is `pytest.raises(ValueError, ...)` failing because an
`IntegrityError` propagated instead — an unambiguous test failure,
confirming the Python-level guard is what the test depends on (the DB
constraint is defense in depth, not a substitute for it: a caller in
autocommit mode or against a differently-configured connection would
otherwise get a wrong row written before ever hitting a constraint).

**Restored:** `cp` from backup; `diff` identical; re-ran → pass.

### 12. `test_cache_round_trip_and_metadata`

**Property:** `BarCache` round-trips bars through CSV exactly — every
field, including which was `high` and which was `low`.

**Broke:** in `data.py`'s `write_bars`, swapped the write order of
`b.high` and `b.low` in the row (header unchanged, so `DictReader`
faithfully reads the wrong value into the right-named field on load).

**Failure:**
```
assert list(loaded) == bars, "round trip must preserve exact Decimal values"
E       AssertionError: round trip must preserve exact Decimal values
E         At index 0 diff: Bar(..., high=Decimal('10.123456'), low=Decimal('10.2'), ...) != Bar(..., high=Decimal('10.2'), low=Decimal('10.123456'), ...)
```

**Restored:** `cp` from backup; `diff` identical; re-ran → pass.

### 13. `test_max_drawdown_and_sample_stats_math`

**Property:** max drawdown is `(peak - trough) / peak` — a fraction of
the PEAK, not of the trough.

**Broke:** in `scoring.py`'s `max_drawdown`, changed
`dd = (peak - value) / peak` to `dd = (peak - value) / value`.

**Failure:**
```
assert max_drawdown([D(x) for x in (100, 120, 90, 110, 80)]) == D("40") / D("120")
E       AssertionError: assert Decimal('0.5') == (Decimal('40') / Decimal('120'))
```

**Restored:** `cp` from backup; `diff` identical; re-ran → pass.

### 14. `test_random_strategy_on_trending_data_still_lags_buy_and_hold`

**Property:** the harness must not manufacture edge — a random
long-only strategy trading a genuinely uptrending synthetic universe
must still lag buy-and-hold of the same index (it sits partly in cash
and pays costs; buy-and-hold does neither).

**First sabotage attempt (informative negative — did not flip the
sign, moved on):** set `cost = ZERO` unconditionally in `harness.py`,
ignoring `cfg.per_side_cost_pct`. Test still passed — cash drag from
equal-weight sizing alone was enough to keep the random strategy behind
buy-and-hold even with zero costs on this fixture, so this particular
bug wasn't the one this particular test is positioned to catch.

**Second attempt:** changed equal-weight sizing
(`budget = equity / cfg.max_positions`) to `budget = equity` (ignoring
`cfg.max_positions`), concentrating each entry into the full account.
This did break the test, but only via the `len(detail.trades) >= 20`
sample-size guard (concentrated sizing exhausted settled cash faster,
producing more `insufficient_settled_cash` skips) — a real failure, but
not the one demonstrating "manufactured edge" via `excess_return_net`.

**Third attempt — the one that hits the intended property directly:**
in `harness.py`'s entry handling, removed `settled_cash -= notional`
after computing `qty` — i.e. buying a position no longer costs
anything (classic "free money" bug: notional is spent on shares but
never leaves the cash balance, so it's double-counted every time).

**Failure:**
```
assert b.excess_return_net < 0, (...)
E       AssertionError: random strategy shows edge over its own index (excess 1271.082878116481161912048637) - the harness is lying somewhere
```
An excess return of +127,000% makes the defect impossible to miss —
exactly the "the harness is lying somewhere" signature this test
exists to catch.

**Restored:** `cp` from backup; `diff` identical; re-ran → pass.

---

## Stage 2 summary

13 tests directly negative-controlled; all 13 caught their sabotage
(three needed the trade-list-based indirect failure documented above,
which is still an unambiguous, sabotage-caused failure). One test
(`test_in_out_of_sample_split_is_chronological`) was found incapable of
catching a reversed/swapped split on its original fixture and was
strengthened with a content-based assertion (linear-ramp price series +
independently-computed expected means); the strengthened version was
verified to pass on correct code and fail on the reversed-split
sabotage before being kept.

No new missing-property gaps were found beyond the one fixed above; the
existing 16-test file already covers look-ahead, fill timing, T+1
settlement, position-slot capping, forced end-of-range exit, benchmark
math (both the open-vs-close and the excess-return sign), cost
direction, chronological split, survivorship-statement persistence,
full round-trip persistence (including mode validation), cache
round-trip fidelity, drawdown/sample-stat arithmetic, and the
random-strategy null-edge check.

`python3 -m pytest tests/ -q` → 35 passed (16 in test_backtest.py, 19
in test_scaffold.py). `catalyst/backtest/{data,harness,scoring}.py` are
byte-identical to the pre-sabotage backup taken before this pass began
(verified with `diff`, and independently by diffing against
backtest-engineer's own pre-existing working-tree changes — nothing
from this session survived in those three files). The only source
change kept from this session is the strengthened assertion block in
`tests/test_backtest.py::test_in_out_of_sample_split_is_chronological`.

## Stage 5 (risk, execution, boundary) — 2026-08-10

- sizing.py spread gate doubled (`> bound*2`): caught by
  test_spread_gate_is_hard_and_binding. Restored, green.
- adaptive_params.py loosen asymmetry removed (loosen step = tighten
  step): caught by test_loosen_step_is_a_third_of_tighten AND
  test_auto_revert_on_opposing_post_sample. Restored, green.
- orders.py cancel-confirmation forced true (`if True:`): caught by
  test_unconfirmed_cancel_places_nothing (the double-stop invariant).
  Restored, green.
- boundary.py forced tool_choice relaxed to auto: caught by
  test_happy_path_two_turns. Restored, green.
- boundary.py transport called before authorize: caught by
  test_budget_denied_before_any_call. Restored, green.
  **Incident:** after restore the test kept failing — the restored file
  was byte-identical but Python reused the sabotaged .pyc (the swap
  reorders identical bytes: same size, same mtime second). Cleared
  __pycache__, suite green. Lesson: verify sabotage restores with a
  cache clear, not a diff alone.
- cycle.py kill-switch early return removed: caught by
  test_broker_down_trips_and_stops_everything. Restored (with cache
  clear), green.
- refusal_tracker.py evidence sign flipped (profitable refusals would
  RAISE the conviction floor): caught by
  test_profitable_refusals_push_floor_down. Restored, green.
- adaptive_params.py F3 fix undone (reverted rows hidden from the
  window check): caught by
  test_reverted_adjustments_window_still_blocks_reuse. Restored, green.
- cycle.py kill-trip protective duties removed (B2 fix undone): caught
  by test_loss_trip_still_runs_exits_but_blocks_entries. Restored, green.
- Re-review batch (NEW-1..6, B4 residuals, F1 dedupe, escalations
  1/2/4/5/6/8/9): six of the stress-tester's xfail escalation tests
  flipped to PASSING purely by applying the fixes (the tests were
  written first and carried the desired behavior) - the equivalent of
  a failing-test-first cycle for each. Markers removed so they are
  permanent regressions.
- Risk round-3 batch: two-pass 404 terminalization sabotaged (single
  404 terminalizes) -> caught by both new reconcile tests; done_for_day
  removed from the void list -> caught; intraday-high max reverted to
  replace -> caught. The duplicate-reduction stale-live and float-
  netting fixes were proven by test-writer's pre-written failing tests
  flipping to green (markers then removed).
