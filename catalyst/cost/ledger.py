"""Scheduled vs manual spend ledger and monthly rollup.

Deliberately exposes NO function that multiplies a partial-month figure
into an annual estimate (ARCHITECTURE.md section 7.4) - annualizing is
refused, not performed.
"""


def month_to_date_cents(kind: str) -> int:
    raise NotImplementedError("stage 3")
