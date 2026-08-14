# Traps that have already cost real money

Read this before writing any cost tracking, data feed, or broker
code. Each entry below was discovered the expensive way.

Short list, all learned the hard way. Everything else is your call.

**Cost tracking**
- The Anthropic Cost API reports **whole days only**. Today's spend is not
  queryable until the day closes — a naive reading shows $0 and looks like
  a broken account.

  **Owner decision, 2026-08-14: a daily figure is fine.** The lag is
  accepted — if a day's true cost lands slightly differently from the
  local estimate, the budget simply re-bases the next day. So the
  reconciliation is a *correction*, not an alarm, and a discrepancy is
  not by itself evidence of a fault.

  **Implemented 2026-08-14 as "block only if large"** (owner's choice
  when asked). A discrepancy pauses spending only when it clears BOTH an
  absolute floor (`RECONCILE_PAUSE_FLOOR_CENTS`, 50c) AND a real
  fraction of what the window actually cost. The old rule bounded
  *accumulated* drift at five cents over a 30-day window, which halted
  the bot for a day and refused 125 candidates — rounding alone reaches
  five cents.

  What this does **not** mean: the local ledger is still required and is
  not a duplicate of the API. The governor has to know spend *now*, in
  the middle of a day, to decide whether the next call is affordable —
  and the Cost API cannot answer that at any price. Local tracking is
  the only real-time number there is; the API is the end-of-day check on
  it.
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

**Alpaca**
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
  **Correction, verified 2026-08-10 against a live paper account:** the
  endpoint is `/v1/corporate-actions` — **`/v2/corporate-actions` 404s**,
  easy to hit by extrapolating from the `/v2/` prefix every other Alpaca
  data endpoint uses. `types=merger` is not a valid filter; the real enum
  (readable from the API's own 400 error body) is `forward_split`,
  `reverse_split`, `stock_dividend`, `spin_off`, `cash_merger`,
  `stock_merger`, `stock_and_cash_merger`, `unit_split`, `cash_dividend`,
  `redemption`, `name_change`, `worthless_removal`, `rights_distribution`,
  `contract_adjustment`, `partial_call`, `reorganization`. The
  dividend-dominance holds (1000+ `cash_dividend` records in a 39-day
  window, more pages beyond that). **The "roughly one usable name a day"
  figure does not hold as measured**: `cash_merger` + `stock_merger` +
  `stock_and_cash_merger` together averaged ~2.2/day over
  2026-07-01–2026-08-09, before any liquidity/materiality filtering.
  Treat "roughly one" as this project's original estimate, not a
  re-confirmed number — re-measure before sizing anything off it. (The
  identical line also appears in BUILD-BRIEF.md's traps list; that copy
  has not been corrected.)
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
