# Traps that have already cost real money

Read this before writing any cost tracking, data feed, or broker
code. Each entry below was discovered the expensive way.

Short list, all learned the hard way. Everything else is your call.

**Cost tracking**
- The Anthropic Cost API reports **whole days only**. Today's spend is not
  queryable until the day closes — a naive reading shows $0 and looks like
  a broken account.
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
