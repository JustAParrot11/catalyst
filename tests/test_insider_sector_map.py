"""The backtest arm reads a real sector, and can still run without one.

The graded insider strategy hardcoded sector="unknown", so every
same-week cluster collapsed into ONE correlation key and
max_correlated_cluster_pct capped unrelated companies as a single bet.
Measured in backtest/harness.py: that bound moved out-of-sample excess
return from +10.4% to -20.1%.

The map must be OPTIONAL and absent-safe, because the originally graded
run has to stay reproducible for the two to be comparable.
"""

from datetime import date

import pytest

from catalyst.strategies.insider_cluster import (
    ClusterEvent, build_candidates, load_sector_map,
)
from catalyst.discovery.correlation import cluster_key_for


def ev(symbol, day="2026-08-20"):
    return ClusterEvent(symbol=symbol, filing_date=date.fromisoformat(day),
                        n_insiders=3, total_value_usd=100000.0)


class TestTheMapIsOptional:
    def test_no_map_reproduces_the_originally_graded_run(self):
        """The comparison depends on this. Without a map every candidate
        must key on 'unknown' exactly as it always did."""
        cands, _ = build_candidates([ev("AAA"), ev("BBB")])
        assert [c.sector for c in cands] == ["unknown", "unknown"]

    def test_a_missing_file_is_an_empty_map_not_a_crash(self, tmp_path):
        assert load_sector_map(tmp_path / "nope.csv") == {}

    def test_an_empty_map_behaves_like_no_map(self):
        cands, _ = build_candidates([ev("AAA")], {})
        assert cands[0].sector == "unknown"


class TestTheMapIsUsed:
    def test_a_known_symbol_gets_its_sic(self):
        cands, _ = build_candidates([ev("AAA")], {"AAA": "2834"})
        assert cands[0].sector == "2834"

    def test_an_unknown_symbol_still_falls_back(self):
        """Partial coverage must not break the run - a company we cannot
        place clusters conservatively, which is the old behaviour."""
        cands, _ = build_candidates([ev("AAA"), ev("ZZZ")], {"AAA": "2834"})
        assert [c.sector for c in cands] == ["2834", "unknown"]

    def test_symbol_lookup_is_case_insensitive(self):
        cands, _ = build_candidates([ev("aaa")], {"AAA": "2834"})
        assert cands[0].sector == "2834"

    def test_it_reads_a_two_column_csv(self, tmp_path):
        p = tmp_path / "m.csv"
        p.write_text("symbol,sic\nAAA,2834\nBBB,6021\n")
        assert load_sector_map(p) == {"AAA": "2834", "BBB": "6021"}

    def test_a_blank_sic_reads_as_unknown_not_as_a_sector(self, tmp_path):
        """An empty SIC is a real answer - EDGAR has none for that
        company - and must cluster as unknown, never as the sector ''."""
        p = tmp_path / "m.csv"
        p.write_text("symbol,sic\nAAA,\n")
        cands, _ = build_candidates([ev("AAA")], load_sector_map(p))
        assert cands[0].sector == "unknown"


class TestTheEffectOnClustering:
    def test_two_industries_stop_capping_against_each_other(self):
        """The whole point, end to end."""
        cands, _ = build_candidates([ev("AAA"), ev("BBB")],
                                    {"AAA": "2834", "BBB": "6021"})
        keys = {cluster_key_for(c.sector, c.catalyst_type, c.catalyst_date)
                for c in cands}
        assert len(keys) == 2

    def test_the_same_industry_still_caps_together(self):
        """The bound is not being removed. Two biotechs resolving the
        same week are one bet and must remain one cluster."""
        cands, _ = build_candidates([ev("AAA"), ev("BBB")],
                                    {"AAA": "2834", "BBB": "2834"})
        keys = {cluster_key_for(c.sector, c.catalyst_type, c.catalyst_date)
                for c in cands}
        assert len(keys) == 1

    def test_without_the_map_they_all_collapse_into_one(self):
        """The defect, as a test, so it cannot return quietly."""
        cands, _ = build_candidates([ev("AAA"), ev("BBB"), ev("CCC")])
        keys = {cluster_key_for(c.sector, c.catalyst_type, c.catalyst_date)
                for c in cands}
        assert len(keys) == 1 and "unknown" in next(iter(keys))
