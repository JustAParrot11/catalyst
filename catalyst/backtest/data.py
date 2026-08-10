"""Local bar cache and point-in-time data access for the backtest.

Owner: backtest-engineer.

Design rule 1 — fetch once, replay hundreds of times at zero cost.
The cache is plain CSV (one file per symbol) plus a metadata JSON that
records where every number came from: fetch date, feed, adjustment,
symbol count, and the raw upstream body for any symbol that returned
empty ("no data" and "the query is broken" look identical otherwise).

Design rule 2 — look-ahead must be hard to WRITE, not just discouraged.
Strategy code never touches BarCache directly during a replay; it is
handed a PointInTimeView, whose every accessor filters to bars dated
on or before its `as_of` and raises LookAheadError on any explicit
request past it. There is no method on the view that can return a
future bar.

Network code lives here too (fetch_daily_bars) but is invoked only by
scripts/fetch_history.py — never by tests (the conftest socket guard
enforces that mechanically) and never mid-replay.

Provenance facts this module bakes in (docs/DATA-SOURCES.md, verified
live 2026-08-10):
- SIP daily history begins 2016-01-04. Earlier start dates return
  HTTP 200 with {"bars": null} — that is "no data before SIP start",
  not an error, and the raw body is recorded beside the empty result.
- feed=sip must be passed explicitly; IEX history is fragmentary.
- adjustment=all must be passed explicitly; the default `raw` series
  understates SPY total return by ~67pp over the full window.
"""

from __future__ import annotations

import csv
import json
import os
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

#: First day of Alpaca SIP daily history, measured by probing (DATA-SOURCES.md §1.1).
SIP_START = date(2016, 1, 4)

ALPACA_DATA_URL = "https://data.alpaca.markets"
FEED = "sip"          # never rely on the account default (DATA-SOURCES.md §1.5)
ADJUSTMENT = "all"    # total-return series; `raw` understates SPY by ~67pp (§1.2)


class LookAheadError(RuntimeError):
    """A caller asked a point-in-time view for data after its as_of date."""


@dataclass(frozen=True)
class Bar:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class BarCache:
    """CSV-per-symbol daily bar store under a single root directory.

    The root lives under the repo's .gitignored data/ directory in real
    use; tests point it at a tmp_path and write synthetic fixtures with
    write_bars() — the format is the contract, not the network.
    """

    META_FILE = "cache_meta.json"
    _HEADER = ["date", "open", "high", "low", "close", "volume"]

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._mem: dict[str, tuple[Bar, ...]] = {}

    def _path(self, symbol: str) -> Path:
        return self.root / f"{symbol.upper()}.csv"

    def write_bars(self, symbol: str, bars: list[Bar]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        ordered = sorted(bars, key=lambda b: b.day)
        with self._path(symbol).open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(self._HEADER)
            for b in ordered:
                w.writerow([b.day.isoformat(), b.open, b.high, b.low, b.close, b.volume])
        self._mem.pop(symbol.upper(), None)

    def load_bars(self, symbol: str) -> tuple[Bar, ...]:
        key = symbol.upper()
        if key in self._mem:
            return self._mem[key]
        path = self._path(symbol)
        if not path.exists():
            raise KeyError(
                f"No cached bars for {symbol!r} under {self.root} — run "
                "scripts/fetch_history.py first; the backtest never fetches mid-run."
            )
        out: list[Bar] = []
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                out.append(Bar(
                    day=date.fromisoformat(row["date"]),
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=Decimal(row["volume"]),
                ))
        bars = tuple(sorted(out, key=lambda b: b.day))
        self._mem[key] = bars
        return bars

    def has(self, symbol: str) -> bool:
        return self._path(symbol).exists()

    def symbols(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*.csv"))

    def write_meta(self, meta: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / self.META_FILE).write_text(json.dumps(meta, indent=2, default=str))

    def read_meta(self) -> dict | None:
        path = self.root / self.META_FILE
        if not path.exists():
            return None
        return json.loads(path.read_text())


class PointInTimeView:
    """The only data object strategy code sees during a replay.

    Every accessor filters to bars dated <= as_of. Asking explicitly for
    anything later raises LookAheadError. The underlying cache is held
    privately; there is no accessor that returns it.
    """

    def __init__(self, cache: BarCache, as_of: date):
        self.__cache = cache
        self.__as_of = as_of

    @property
    def as_of(self) -> date:
        return self.__as_of

    def symbols(self) -> list[str]:
        return [s for s in self.__cache.symbols() if s != BarCache.META_FILE]

    def bars(self, symbol: str, start: date | None = None,
             end: date | None = None) -> tuple[Bar, ...]:
        if end is None:
            end = self.__as_of
        if end > self.__as_of:
            raise LookAheadError(
                f"Requested bars for {symbol} through {end}, but the simulated "
                f"clock is {self.__as_of}. The future is not visible."
            )
        if start is not None and start > end:
            return ()
        all_bars = self.__cache.load_bars(symbol)
        # Defense in depth: filter on as_of as well as the checked `end`.
        return tuple(
            b for b in all_bars
            if b.day <= min(end, self.__as_of)
            and (start is None or b.day >= start)
        )

    def last_bar(self, symbol: str) -> Bar | None:
        all_bars = self.__cache.load_bars(symbol)
        days = [b.day for b in all_bars]
        i = bisect_right(days, self.__as_of)
        return all_bars[i - 1] if i else None

    def last_close(self, symbol: str) -> Decimal | None:
        bar = self.last_bar(symbol)
        return bar.close if bar else None


# ---------------------------------------------------------------------------
# Network fetch — used ONLY by scripts/fetch_history.py, never by tests
# and never during a replay.
# ---------------------------------------------------------------------------

def alpaca_auth_headers() -> dict[str, str]:
    """Read credentials from the environment BY NAME.

    The values are never printed, logged, or persisted anywhere by this
    module — they go straight into request headers and nowhere else.
    """
    key = os.environ.get("ALPACA_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(
            "ALPACA_KEY and/or ALPACA_SECRET_KEY are not set in the environment. "
            "Export them (never commit them) and re-run."
        )
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def fetch_daily_bars(client, symbols: list[str], start: date, end: date,
                     ) -> tuple[dict[str, list[Bar]], list[dict]]:
    """Fetch daily bars for a chunk of symbols, following pagination.

    Returns (bars_by_symbol, notes) where notes carries the RAW upstream
    body for every empty/odd response — a zero is never left unexplained.
    A start before SIP_START legitimately yields {"bars": null} (HTTP
    200); that is recorded as "no data before SIP start", not an error.

    `client` is an httpx.Client (passed in so the caller owns pacing and
    lifecycle; the 200 req/min limit is the caller's to respect).
    """
    bars_by_symbol: dict[str, list[Bar]] = {}
    notes: list[dict] = []
    params = {
        "symbols": ",".join(symbols),
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "adjustment": ADJUSTMENT,
        "feed": FEED,
        "limit": 10000,
    }
    page_token: str | None = None
    returned_symbols: set[str] = set()
    while True:
        q = dict(params)
        if page_token:
            q["page_token"] = page_token
        resp = client.get(f"{ALPACA_DATA_URL}/v2/stocks/bars", params=q)
        resp.raise_for_status()
        body = resp.json()
        raw_bars = body.get("bars")
        if raw_bars is None:
            notes.append({
                "symbols": symbols,
                "note": ("bars=null — no data in window (normal for start before "
                         f"SIP floor {SIP_START.isoformat()}; DATA-SOURCES.md §1.5)"),
                "raw_body": body,
            })
            break
        returned_symbols.update(raw_bars)
        for sym, rows in raw_bars.items():
            dest = bars_by_symbol.setdefault(sym, [])
            for r in rows:
                ts = datetime.fromisoformat(r["t"].replace("Z", "+00:00"))
                dest.append(Bar(
                    day=ts.astimezone(timezone.utc).date(),
                    open=Decimal(str(r["o"])),
                    high=Decimal(str(r["h"])),
                    low=Decimal(str(r["l"])),
                    close=Decimal(str(r["c"])),
                    volume=Decimal(str(r["v"])),
                ))
        page_token = body.get("next_page_token")
        if not page_token:
            break
    for sym in symbols:
        if sym not in bars_by_symbol:
            notes.append({
                "symbols": [sym],
                "note": ("symbol absent from an otherwise-populated response — "
                         "likely no trades in window, unlisted, or a bad ticker"),
                "raw_body": {"returned_symbols": sorted(returned_symbols),
                             "requested": symbols},
            })
    return bars_by_symbol, notes
