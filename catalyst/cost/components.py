"""Split a day's bill into its priced components, or refuse to.

The blended correction in `measured_rates` asks one question of a whole
day - "did it cost more than we thought?" - and scales the input and
output rates by the answer. That tracks any price change, but it smears
it: a change to the cache-read multiplier alone comes out as a small
move in the token rates, landing close rather than exact.

Anthropic itemises the bill by `token_type`. If those lines can be read,
each component has its own measured price and nothing has to be assumed:
the cache multipliers and the web-search charge become measurements like
everything else.

THE PROBLEM THIS MODULE IS BUILT AROUND. The exact `token_type`
vocabulary is not verifiable from here, and this project's own recorded
response from the owner's account carried every breakdown field as null.
So the matching below is a guess about somebody else's API, and a guess
about which line means what is precisely how a cost line gets silently
dropped and the bill understated - the TRAPS.md renamed-field trap in a
new hat.

SO NOTHING IS BELIEVED ON SIGHT. classify() refuses in three ways:

  - a line it cannot place is not skipped, it FAILS the whole split;
  - the placed lines must add back up to the day's billed total;
  - anything left over, either way, fails it.

A failed split costs a fallback to the blended factor that already
works. There is no path where a misread line quietly becomes a price.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

#: Matched on MEANING, not on an exact vocabulary (house rule 7). The
#: API's strings are not verifiable from here, so each component is
#: recognised by the words that have to be in it for it to be that
#: component at all - "cache" plus "read", "cache" plus "creation" or
#: "write", and so on. An unrecognised line still fails the split rather
#: than being dropped, so a vocabulary this misses costs a fallback and
#: never a mispricing.
_UNCACHED_INPUT = re.compile(r"uncached.*input|(?<!cache_)input(?!.*cache)", re.I)
_OUTPUT = re.compile(r"output", re.I)
_CACHE_READ = re.compile(r"cache.*read|read.*cache", re.I)
_CACHE_WRITE_1H = re.compile(r"(1h|1_h|hour).*(cache|creation)|cache.*(1h|1_h|hour)", re.I)
_CACHE_WRITE = re.compile(r"cache.*(creation|write)|(creation|write).*cache", re.I)
_WEB_SEARCH = re.compile(r"web.?search", re.I)


class ComponentSplitRefused(ValueError):
    """The day's bill could not be split into components it can price.

    Carries `why` so the reason reaches the dashboard rather than
    becoming a silent fallback - "the split was refused" and "there was
    no breakdown at all" are different facts and need telling apart.
    """

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


@dataclass(frozen=True)
class BilledComponents:
    """One closed day's money, by what it was spent on. Cents."""

    uncached_input: Decimal = Decimal("0")
    output: Decimal = Decimal("0")
    cache_write: Decimal = Decimal("0")
    cache_write_1h: Decimal = Decimal("0")
    cache_read: Decimal = Decimal("0")
    web_search: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return (self.uncached_input + self.output + self.cache_write
                + self.cache_write_1h + self.cache_read + self.web_search)


def _label(record: dict) -> str:
    """Everything on the record that might say what the line is for.

    Reads token_type AND description AND cost_type together rather than
    picking one: which of them carries the meaning is exactly the part
    that cannot be checked from here, and a line that names itself in
    any of them is a line that can be placed.
    """
    return " ".join(
        str(record.get(k) or "")
        for k in ("token_type", "description", "cost_type"))


def classify(records: list[dict], billed_total: Decimal,
             tolerance: Decimal) -> BilledComponents:
    """Split `records` into components, or raise ComponentSplitRefused.

    `billed_total` is the day's money as the reconciliation already
    computed it - the number that has to come back out at the end.
    """
    if not records:
        raise ComponentSplitRefused(
            "the Cost API returned no records to split")

    out = {"uncached_input": Decimal("0"), "output": Decimal("0"),
           "cache_write": Decimal("0"), "cache_write_1h": Decimal("0"),
           "cache_read": Decimal("0"), "web_search": Decimal("0")}
    unplaced: list[str] = []

    for rec in records:
        try:
            amount = Decimal(str(rec.get("amount")))
        except (InvalidOperation, TypeError, ValueError):
            raise ComponentSplitRefused(
                f"a cost line carried an unreadable amount: {str(rec)[:200]}"
            ) from None
        label = _label(rec).strip()
        if not label:
            unplaced.append(f"(no token_type/description) {amount}c")
            continue
        # ORDER MATTERS: the 1h cache line also matches the general cache
        # write pattern, and a cache line also contains the word "input".
        # Most specific first, so a line lands in exactly one bucket.
        if _WEB_SEARCH.search(label):
            out["web_search"] += amount
        elif _CACHE_READ.search(label):
            out["cache_read"] += amount
        elif _CACHE_WRITE_1H.search(label):
            out["cache_write_1h"] += amount
        elif _CACHE_WRITE.search(label):
            out["cache_write"] += amount
        elif _OUTPUT.search(label):
            out["output"] += amount
        elif _UNCACHED_INPUT.search(label):
            out["uncached_input"] += amount
        else:
            unplaced.append(f"{label!r} {amount}c")

    if unplaced:
        # NOT DROPPED. A line nobody recognised is the whole reason this
        # refuses rather than returning its best effort.
        raise ComponentSplitRefused(
            "the bill carried cost lines this code cannot place, and a "
            "line treated as zero would understate the bill: "
            + "; ".join(unplaced[:5]))

    got = BilledComponents(**out)
    if billed_total <= 0:
        raise ComponentSplitRefused(
            "the day's billed total is not positive, so a split cannot be "
            "checked against it")
    drift = (got.total - billed_total).copy_abs() / billed_total
    if drift > tolerance:
        raise ComponentSplitRefused(
            f"the split came to {got.total}c against a billed {billed_total}c "
            f"({drift * 100:.2f}% out, over the {tolerance * 100:.2f}% "
            "allowed) - so the lines were read wrongly and are not trusted")
    return got
