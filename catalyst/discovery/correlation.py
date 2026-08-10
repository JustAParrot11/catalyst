"""Concentration clustering: sector + catalyst_type + resolution-week.

risk.evaluate() consumes the keys this module mints, not raw ticker
count, to judge whether several positions are one bet wearing several
hats (ARCHITECTURE 9.7). Authored by strategy-analyst, reviewed by
risk-reviewer.

Key semantics:
- Two insider-cluster candidates in the same sector whose clusters
  resolved in the same ISO week share a key — names reacting to the
  same week's information are correlated (STRATEGY-PROPOSALS 7.1).
- Sector is lower-cased and empty falls back to "unknown". Form 4
  payloads carry no sector today, so most insider candidates land in
  "unknown" — which collapses every same-week insider cluster into ONE
  cluster key. That is deliberate and conservative: when we cannot
  tell names apart, risk treats them as correlated and
  max_correlated_cluster_pct caps their combined exposure. Sector
  enrichment can only loosen this, never tighten it.

open_positions is part of the frozen interface (ARCHITECTURE 3.2) and
is how the risk engine's view stays consistent: an OpenPosition carries
the cluster_key THIS function minted when the position was opened, and
because cluster_key_for() is a pure deterministic function of
(sector, catalyst_type, catalyst_date), a new candidate that belongs to
the same sector/type/week as an open position reproduces the identical
string, and sizing's max_correlated_cluster check matches them by
equality. No state is needed from the positions to mint a key.
"""

from __future__ import annotations

from datetime import date

from catalyst.discovery import Candidate


def resolution_week(d: date) -> str:
    """ISO year-week, e.g. 2026-W32. ISO (not calendar-month) weeks so a
    Friday and the following Monday do not straddle a boundary."""
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def cluster_key_for(sector: str, catalyst_type: str, catalyst_date: date) -> str:
    sector_norm = (sector or "").strip().lower() or "unknown"
    return f"{sector_norm}|{catalyst_type}|{resolution_week(catalyst_date)}"


def cluster(candidates: list[Candidate], open_positions: list) -> dict[str, str]:
    """Returns candidate_id -> cluster_key.

    Pure and deterministic: the same candidate always maps to the same
    key, on any run, so keys stored on open positions (minted here at
    entry time) compare by string equality against new candidates'
    keys in risk/sizing.py. open_positions is accepted per the frozen
    interface; see the module docstring for why no data is read from it.
    """
    return {
        c.id: cluster_key_for(c.sector, c.catalyst_type, c.catalyst_date)
        for c in candidates
    }
