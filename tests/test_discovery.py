"""Offline tests for strategy-analyst's stage-5 files: discovery
candidates, correlation clustering, and the research prompt.

The load-bearing property: live discovery must implement EXACTLY the
cluster definition the backtest graded (catalyst/strategies/
insider_cluster.py), or the backtest graded a different strategy than
the one being traded. Enforced two ways: the threshold constants are
asserted to be imported (not re-typed) from the backtest module, and a
behavioural parity test runs both implementations on identical
synthetic Form 4 data and requires identical events.

SABOTAGE LOG (house rule 4 — a test that cannot fail is not a test;
both breaks were made in a copy of the real module, run, confirmed
caught, then reverted; suite re-run green afterwards):
- Sabotage 1 (2026-08-10): in candidates.py::_parse_purchase, the
  10b5-1 exclusion membership test was changed from ("1", "true") to
  ("never",). Caught by test_10b51_flagged_buys_excluded (2 asserts
  failed: flagged rows produced a candidate) AND by
  test_parity_with_backtest_arm (event sets diverged). Reverted.
- Sabotage 2 (2026-08-10): in prompts.py::render_research_prompt, the
  sentence "Also recommend a sensible position size." was appended to
  GROUND RULES. Caught by test_prompt_has_no_size_shaped_words
  ("position size" found). Reverted.
"""

from __future__ import annotations

import inspect
import re
from datetime import date, datetime, timezone

from catalyst.data import RawEvent
from catalyst.discovery import Candidate
import catalyst.discovery.candidates as candidates_mod
from catalyst.discovery.candidates import build_candidates, candidate_facts
from catalyst.discovery.correlation import cluster, cluster_key_for
from catalyst.research.prompts import exploration_tools, render_research_prompt
from catalyst.strategies import insider_cluster as arm

AS_OF = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def form4(source_id: str, *, issuer: str = "0001111111",
          symbol: str = "ACME", owner: str = "0009000001",
          filing_date: str = "2026-08-01", value: float = 30_000.0,
          aff: str = "", fetched: datetime = AS_OF, **extra) -> RawEvent:
    """One Form 4 purchase RawEvent, payload per the schema
    scripts/fetch_insider_data.py produced for the backtest."""
    payload = {
        "issuer_cik": issuer, "symbol": symbol, "owner_cik": owner,
        "filing_date": filing_date, "trans_date": filing_date,
        "value_usd": f"{value:.2f}", "shares": "1000",
        "shares_owned_after": "11000", "aff10b5one": aff,
    }
    payload.update(extra)
    return RawEvent(source="edgar_form4", source_id=source_id,
                    fetched_at=fetched, payload_raw=payload)


def one_cluster_events() -> list[RawEvent]:
    """Exactly one cluster under the backtest arm's definition: two
    distinct owner CIKs within 10 calendar days, $60k combined."""
    return [
        form4("f4-1", owner="0009000001", filing_date="2026-08-01",
              value=30_000.0, owner_name="Jane Doe", owner_role="CFO"),
        form4("f4-2", owner="0009000002", filing_date="2026-08-03",
              value=30_000.0),
    ]


# ---------------------------------------------------------------- cluster


def test_one_cluster_matches_backtest_definition():
    cands = build_candidates(one_cluster_events(), AS_OF)
    assert len(cands) == 1
    c = cands[0]
    assert c.catalyst_type == "insider_cluster"
    assert c.ticker == "ACME"
    # Tradable/event date is the cluster-completing FILING_DATE.
    assert c.catalyst_date == date(2026, 8, 3)
    assert c.catalyst_date_confidence == "confirmed"
    assert c.source_event_ids == ("f4-1", "f4-2")
    assert c.discovered_at == AS_OF
    facts = candidate_facts(c)
    assert facts["insiders"] == "2"
    assert facts["total_usd"] == "60000"
    assert facts["window"] == "2026-08-01..2026-08-03"
    assert len(facts["buyers"]) == 2


def test_below_thresholds_is_no_candidate():
    # One insider alone, however large: not a cluster (MIN_INSIDERS=2).
    solo = [form4("s1", owner="0009000001", value=500_000.0)]
    assert build_candidates(solo, AS_OF) == []
    # Two insiders but combined $40k < MIN_TOTAL_VALUE_USD=$50k.
    small = [form4("t1", owner="0009000001", value=20_000.0),
             form4("t2", owner="0009000002", filing_date="2026-08-03",
                   value=20_000.0)]
    assert build_candidates(small, AS_OF) == []
    # Same owner twice is one insider, not two.
    same = [form4("u1", owner="0009000001", value=40_000.0),
            form4("u2", owner="0009000001", filing_date="2026-08-03",
                  value=40_000.0)]
    assert build_candidates(same, AS_OF) == []
    # Outside the 10-day window: no cluster.
    far = [form4("w1", owner="0009000001", filing_date="2026-07-01",
                 value=30_000.0),
           form4("w2", owner="0009000002", filing_date="2026-08-01",
                 value=30_000.0)]
    assert build_candidates(far, AS_OF) == []


def test_constants_imported_from_backtest_arm_not_retyped():
    assert candidates_mod.CLUSTER_WINDOW_DAYS == arm.CLUSTER_WINDOW_DAYS
    assert candidates_mod.MIN_INSIDERS == arm.MIN_INSIDERS
    assert candidates_mod.DEDUPE_DAYS == arm.DEDUPE_DAYS
    # Floats are not interned: identity proves the same object, i.e. an
    # import binding rather than a re-typed literal.
    assert candidates_mod.MIN_TOTAL_VALUE_USD is arm.MIN_TOTAL_VALUE_USD
    assert candidates_mod._valid_symbol is arm._valid_symbol
    # And the source must not re-assign any of the shared names.
    src = inspect.getsource(candidates_mod)
    assert "from catalyst.strategies.insider_cluster import" in src
    for name in ("CLUSTER_WINDOW_DAYS", "MIN_INSIDERS",
                 "MIN_TOTAL_VALUE_USD", "DEDUPE_DAYS"):
        assert re.search(rf"^\s*{name}\s*=", src, re.M) is None, (
            f"{name} is re-typed in candidates.py; import it from "
            "strategies/insider_cluster.py instead")


def test_parity_with_backtest_arm(tmp_path):
    """Both implementations, identical input, identical events.

    Covers: a valid 2-insider cluster, the DEDUPE_DAYS collapse, a
    10b5-1-flagged near-cluster, a below-value pair, an invalid symbol,
    and a solo buyer.
    """
    rows = [
        # issuer 1: cluster completes 08-03; third filing 08-12 is
        # inside DEDUPE_DAYS of it and must NOT mint a second event.
        ("0001111111", "ACME", "0009000001", "2026-08-01", 30_000.0, ""),
        ("0001111111", "ACME", "0009000002", "2026-08-03", 30_000.0, ""),
        ("0001111111", "ACME", "0009000003", "2026-08-12", 90_000.0, ""),
        # issuer 2: would qualify but second leg is 10b5-1 flagged.
        ("0002222222", "BOLT", "0009000004", "2026-08-01", 40_000.0, ""),
        ("0002222222", "BOLT", "0009000005", "2026-08-02", 40_000.0, "1"),
        # issuer 3: two insiders, combined below $50k.
        ("0003333333", "CARP", "0009000006", "2026-08-01", 20_000.0, ""),
        ("0003333333", "CARP", "0009000007", "2026-08-02", 20_000.0, ""),
        # issuer 4: invalid symbol (digit) — both filters must drop it.
        ("0004444444", "BAD1", "0009000008", "2026-08-01", 60_000.0, ""),
        ("0004444444", "BAD1", "0009000009", "2026-08-02", 60_000.0, ""),
        # issuer 5: one insider only.
        ("0005555555", "DOVE", "0009000010", "2026-08-01", 999_000.0, ""),
    ]
    # Backtest path: the purchases.csv shape fetch_insider_data.py wrote.
    csv_path = tmp_path / "purchases.csv"
    header = ("issuer_cik,symbol,owner_cik,filing_date,trans_date,"
              "value_usd,shares,shares_owned_after,aff10b5one\n")
    with csv_path.open("w") as f:
        f.write(header)
        for issuer, sym, owner, fd, val, aff in rows:
            f.write(f"{issuer},{sym},{owner},{fd},{fd},{val:.2f},"
                    f"1000,11000,{aff}\n")
    backtest_events = {
        (ev.symbol, ev.filing_date, ev.n_insiders, round(ev.total_value_usd, 2))
        for ev in arm.build_cluster_events(csv_path)
    }
    # Live path: the same rows as RawEvents, as_of past every filing.
    raw = [form4(f"p{i}", issuer=r[0], symbol=r[1], owner=r[2],
                 filing_date=r[3], value=r[4], aff=r[5])
           for i, r in enumerate(rows)]
    live_events = set()
    for c in build_candidates(raw, datetime(2026, 12, 31, tzinfo=timezone.utc)):
        facts = candidate_facts(c)
        live_events.add((c.ticker, c.catalyst_date,
                         int(facts["insiders"]),
                         round(float(facts["total_usd"]), 2)))
    assert backtest_events == live_events
    assert live_events == {("ACME", date(2026, 8, 3), 2, 60_000.0)}


def test_10b51_flagged_buys_excluded():
    for flag in ("1", "true", "TRUE"):
        events = one_cluster_events()
        flagged = events[1].payload_raw | {"aff10b5one": flag}
        events[1] = RawEvent(source="edgar_form4", source_id="f4-2",
                             fetched_at=AS_OF, payload_raw=flagged)
        assert build_candidates(events, AS_OF) == [], (
            f"10b5-1 flag {flag!r} must exclude the purchase")
    # Blank flag (pre-2023 rows) passes — matching the backtest's
    # documented treatment: noise in, never look-ahead.
    assert len(build_candidates(one_cluster_events(), AS_OF)) == 1


# --------------------------------------------------------- point in time


def test_point_in_time_excludes_filings_after_as_of():
    events = one_cluster_events()
    # At 08-02 the second filing (08-03) does not exist yet.
    before = datetime(2026, 8, 2, 23, 59, tzinfo=timezone.utc)
    assert build_candidates(events, before) == []
    # Filed AT as_of counts (at-or-before).
    at = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    assert len(build_candidates(events, at)) == 1


def test_non_form4_sources_ignored():
    stray = RawEvent(source="federal_register", source_id="fr-1",
                     fetched_at=AS_OF,
                     payload_raw=one_cluster_events()[0].payload_raw)
    assert build_candidates([stray] * 3, AS_OF) == []


# ------------------------------------------------------- deterministic id


def test_candidate_id_deterministic_across_reruns():
    a = build_candidates(one_cluster_events(), AS_OF)[0]
    # Re-run: reversed event order, different fetched_at, later as_of —
    # the same cluster must rediscover the same id (no duplicate
    # research on re-runs).
    later = datetime(2026, 8, 7, 6, 0, tzinfo=timezone.utc)
    shuffled = [RawEvent(source=e.source, source_id=e.source_id,
                         fetched_at=later, payload_raw=e.payload_raw)
                for e in reversed(one_cluster_events())]
    b = build_candidates(shuffled, later)[0]
    assert a.id == b.id
    # A materially different cluster gets a different id.
    other = one_cluster_events()
    changed = other[1].payload_raw | {"owner_cik": "0009000099"}
    other[1] = RawEvent(source="edgar_form4", source_id="f4-2",
                        fetched_at=AS_OF, payload_raw=changed)
    c = build_candidates(other, AS_OF)[0]
    assert c.id != a.id


# ------------------------------------------------------------ correlation


def _cand(cid: str, sector: str, cat_date: date,
          catalyst_type: str = "insider_cluster") -> Candidate:
    return Candidate(id=cid, ticker="TST", catalyst_type=catalyst_type,
                     catalyst_date=cat_date,
                     catalyst_date_confidence="confirmed",
                     source_event_ids=("x",), discovered_at=AS_OF,
                     sector=sector, correlation_tags=())


def test_correlation_key_collides_for_same_sector_and_week():
    # 2026-08-03 (Mon) and 2026-08-07 (Fri) share ISO week 2026-W32.
    a = _cand("a", "biotech", date(2026, 8, 3))
    b = _cand("b", "biotech", date(2026, 8, 7))
    keys = cluster([a, b], open_positions=[])
    assert keys["a"] == keys["b"]
    assert keys["a"] == "biotech|insider_cluster|2026-W32"


def test_correlation_key_distinct_otherwise():
    base = _cand("base", "biotech", date(2026, 8, 3))
    other_sector = _cand("s", "energy", date(2026, 8, 3))
    # 2026-08-10 is the following ISO week (W33).
    other_week = _cand("w", "biotech", date(2026, 8, 10))
    other_type = _cand("t", "biotech", date(2026, 8, 3),
                       catalyst_type="earnings_drift")
    keys = cluster([base, other_sector, other_week, other_type], [])
    assert len({keys["base"], keys["s"], keys["w"], keys["t"]}) == 4


def test_unknown_sector_collapses_conservatively():
    # Form 4 payloads carry no sector; every same-week insider cluster
    # must share one key so risk treats them as one bet.
    built = build_candidates(one_cluster_events(), AS_OF)[0]
    assert built.sector == "unknown"
    key = cluster([built], [])[built.id]
    assert key == cluster_key_for("", "insider_cluster",
                                  built.catalyst_date)
    assert key == "unknown|insider_cluster|2026-W32"


# ----------------------------------------------------------------- prompt


def test_prompt_contains_candidate_facts():
    c = build_candidates(one_cluster_events(), AS_OF)[0]
    prompt = render_research_prompt(c)
    assert c.ticker in prompt
    assert "Jane Doe (CFO)" in prompt            # who bought, with role
    assert "CIK 0009000002" in prompt            # nameless buyer fallback
    assert "$30,000" in prompt                   # how much, per buyer
    assert "$60,000" in prompt                   # combined
    assert "2026-08-03" in prompt                # cluster completion date
    assert "no_trade" in prompt                  # refusal offered freely
    assert "priced_in" in prompt
    assert "invalidation" in prompt


def test_prompt_has_no_size_shaped_words():
    c = build_candidates(one_cluster_events(), AS_OF)[0]
    for prompt in (render_research_prompt(c),
                   render_research_prompt(c, graph_context="ctx")):
        low = prompt.lower()
        for forbidden in ("shares to buy", "position size", "dollar amount",
                          "notional", "order size", "how many shares"):
            assert forbidden not in low, (
                f"prompt invites a size: contains {forbidden!r}")


def test_prompt_graph_context_marked_and_optional():
    c = build_candidates(one_cluster_events(), AS_OF)[0]
    chain = 'company "ACME" --files--> filing "Form 4" on 2026-08-03'
    with_ctx = render_research_prompt(c, graph_context=chain)
    assert "evidence graph context (informational only)" in with_ctx.lower()
    assert chain in with_ctx
    without = render_research_prompt(c)          # graph_context=None
    assert "evidence graph context" not in without.lower()
    assert chain not in without


def test_exploration_tools_is_web_search_only():
    tools = exploration_tools()
    assert tools == [{"type": "web_search_20250305", "name": "web_search",
                      "max_uses": 3}]


class TestTheSafetyInstructionsArePRESENT:
    """risk-reviewer, 2026-08-13: every instruction below can be DELETED
    with a green suite, because test_prompt_has_no_size_shaped_words
    only checks the prompt does not CONTAIN bad words - nothing checks
    the guards are there at all. Verified by sabotage: deleting both
    safety lines produced zero prompt-related failures.

    These are the only textual defence against model-authored price
    levels. The schema blocks size-shaped FIELDS; it does not block a
    stop level written into `thesis` - and thesis and invalidation are
    replayed verbatim into the position-review prompt, which drives an
    exit. So a model-authored level can propagate model -> text ->
    model -> early exit.
    """

    def _prompt(self):
        return render_research_prompt(
            build_candidates(one_cluster_events(), AS_OF)[0])

    def test_the_model_is_told_not_to_name_levels(self):
        text = self._prompt()
        assert "Report judgements, not instructions" in text
        assert "no order, entry, stop or exit levels" in text

    def test_the_opener_says_the_answer_is_advisory(self):
        """The architecture rule that is not negotiable: the model
        proposes, deterministic code disposes."""
        text = self._prompt()
        assert "advisory only" in text
        assert "deterministic code" in text

    def test_no_trade_is_encouraged_not_merely_permitted(self):
        """Everything downstream is a threshold on model-reported
        conviction, and the refusal tracker only samples refusals tagged
        below_conviction_floor. If conviction inflates, more candidates
        clear the floor AND the evidence that would detect the inflation
        dries up - the loop degrades exactly when it is needed."""
        text = self._prompt()
        assert "Say no_trade freely" in text
        assert "a bad trade costs real money" in text

    def test_the_holding_period_is_anchored(self):
        """expected_holding_days is a live model-to-timing channel: it
        sets the hard exit date, clamped only at 31 days. Without an
        anchor, views drift to the bound and days-at-risk of an
        overnight gap roughly triples."""
        text = self._prompt()
        assert "days to weeks" in text
        assert "expected_holding_days" in text

    def test_the_model_is_told_to_submit_WITHOUT_being_asked(self):
        """token-optimizer, 2026-08-13. boundary.py:202 offers the schema
        tool DURING exploration so a view submitted while the search
        results are in hand skips the forced extraction turn - which
        re-sends the ENTIRE context, 24k tokens on the measured live
        call, to collect a few hundred tokens of JSON. That is 38% of a
        candidate's cost.

        The old wording, "When asked, submit your conclusion", told the
        model to WAIT for a prompt that only the extraction turn issues -
        defeating the short-circuit the code was built for. The saving
        exists only while the prompt does not reinstate the wait.
        """
        text = self._prompt()
        assert "Do not wait to be asked" in text
        assert "When asked, submit" not in text, (
            "this wording tells the model to wait for the extraction "
            "turn, which is the full-price context re-read")

    def test_thesis_and_invalidation_are_still_asked_for(self):
        """The risk engine never reads these, so they look like pure
        audit cost. They are the entire input to the position review,
        and invalidation_triggered is meaningless without a specific
        invalidation written at entry."""
        text = self._prompt()
        assert "thesis" in text
        assert "invalidation" in text
        assert "prove the thesis wrong" in text or "invalidation" in text
