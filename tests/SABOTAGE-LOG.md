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
