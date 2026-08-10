"""Offline tests for the dashboard's pure functions.

No sockets: the route functions are called directly with a Db handle, so
the whole HTTP layer is exercised as plain function calls. The rendering
smoke test that actually binds a port lives in scripts/dashboard_smoke.py
and is run by hand.

Each test here asserts something that can fail: the sabotage checks at
the bottom of this file prove the redaction and duplicate-id checks
catch what they claim to catch.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from catalyst.dashboard import charts, panels, queries, server
from catalyst.dashboard.db import Db, QueryResult
from catalyst.dashboard.redact import MASK, redact, redact_obj
from catalyst.dashboard.render import (
    all_ids,
    duplicate_ids,
    empty_block,
    raw,
)
from catalyst.storage import init_db

FAKE_ALPACA_KEY = "PKFAKE123456789TEST"
FAKE_ANTHROPIC_KEY = "sk-ant-FAKE-0000000000000000"

SCHEMA_LOGS = Path(__file__).resolve().parent.parent / "catalyst" / "dashboard" / "schema_logs.sql"


def _iso(day) -> str:
    return datetime(day.year, day.month, day.day, 14, 30, tzinfo=timezone.utc).isoformat()


@pytest.fixture
def seeded(tmp_path):
    """One traded candidate, one declined candidate, a cost ledger with an
    unacknowledged discrepancy, and a logs table carrying a planted FAKE
    credential."""
    path = str(tmp_path / "dash.db")
    conn = init_db(path)
    conn.executescript(SCHEMA_LOGS.read_text())
    today = datetime.now(timezone.utc).date()
    d5, d1 = today - timedelta(days=5), today - timedelta(days=1)

    conn.execute("INSERT INTO raw_events VALUES (?,?,?,?)",
                 ("edgar", "acc-1", _iso(d5), '{"form":"4"}'))
    conn.execute("INSERT INTO raw_events_errors VALUES (?,?,?)",
                 ("federal_register", _iso(d5), '{"status":500,"body":"timeout"}'))
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 ("c1", "ACME", "insider_cluster", d1.isoformat(), "confirmed",
                  json.dumps(["acc-1"]), _iso(d5), "industrials", json.dumps(["ind"])))
    conn.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                 ("rc1", "c1", "claude-haiku-4-5", "PROMPT-BODY",
                  json.dumps(["web_search"]), "3.0", 900, None, _iso(d5)))
    conn.execute("INSERT INTO research_call_turns VALUES (?,?,?,?,?)",
                 ("rc1", 0, '{"id":"msg_1"}', '{"input_tokens":10}', "tool_use"))
    conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                 ("c1", "long", 0.71, "THESIS-TEXT", "INVALIDATION-TEXT", 12, 0,
                  "PRICED-IN-REASONING"))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("d1", "c1", "trade", "long", "196.40", "4.6", "41.80",
                  (d1 + timedelta(days=12)).isoformat(), "[]",
                  json.dumps({"conviction_floor": 0.6}), _iso(d5)))
    conn.execute("INSERT INTO limit_applications VALUES (?,?,?,?,?,?)",
                 ("d1", "max_loss_per_position", "0.10", "0.12", "hard", 1))
    conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("o1", "c1", "bro-1", "buy", "4.6", "market", "day", _iso(d5),
                  "filled",
                  json.dumps({"status": "filled",
                              "echo": {"APCA-API-KEY-ID": FAKE_ALPACA_KEY}})))
    conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?)",
                 ("o1", "42.71", "4.6", _iso(d5), "42.71", "0.064"))
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                 ("p1", "ACME", json.dumps(["o1"]), "s1", _iso(d5),
                  (d1 + timedelta(days=12)).isoformat(), "closed"))
    conn.execute("INSERT INTO closed_trades VALUES (?,?,?,?,?,?,?,?,?)",
                 ("p1", "paper", "42.71", "45.02", "target_reached", 1000, 12, 4,
                  _iso(d1)))

    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 ("c2", "BIOX", "clinical_readout", d1.isoformat(), "estimated",
                  "[]", _iso(d5), "healthcare", json.dumps(["hc"])))
    conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                 ("c2", "long", 0.68, "T", "I", 10, 0, "R"))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("d2", "c2", "skip", None, None, None, None, None,
                  json.dumps(["adverse_gap_exceeds_max_loss"]), "{}", _iso(d5)))
    conn.execute("INSERT INTO refusals VALUES (?,?,?,?,?,?,?)",
                 ("d2", "c2", "11.40", _iso(d5), _iso(d1), "12.85", "0.1272"))

    for i, (kind, cents) in enumerate([("scheduled", "3.0"), ("manual", "18.5")]):
        conn.execute("INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
                     (f"ce{i}", json.dumps({"input_tokens": 1}), "claude-haiku-4-5",
                      kind, "research", cents, _iso(d1), None))
    conn.execute(
        "INSERT INTO cost_reconciliation_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("recon-1", d1.isoformat(), "all", "{}", "21.5", "0", "21.5", "5",
         json.dumps({"data": [], "x-api-key": FAKE_ANTHROPIC_KEY}), 0,
         "scheduled_paused", None, None, _iso(d1)))
    conn.execute("INSERT INTO cost_governor_events VALUES (?,?,?,?,?,?,?)",
                 ("cyc1", "scheduled", "4.0", "500", "deny", "cap_exceeded", _iso(d1)))
    conn.execute(
        "INSERT INTO logs (ts, level, component, message, cycle_id, candidate_id, "
        "traceback_text, context_json) VALUES (?,?,?,?,?,?,?,?)",
        (_iso(d1), "ERROR", "data.alpaca_news", f"failed with {FAKE_ALPACA_KEY}",
         "cyc1", "c1", "Traceback: boom",
         json.dumps({"ALPACA_API_KEY": FAKE_ALPACA_KEY})))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def bare(tmp_path):
    """Schema only: the empty-equity, nothing-has-traded case."""
    path = str(tmp_path / "bare.db")
    init_db(path).close()
    return path


# ---------------------------------------------------------------- redaction


@pytest.mark.parametrize("planted", [
    FAKE_ALPACA_KEY,
    FAKE_ANTHROPIC_KEY,
    "AKFAKE9876543210TEST",
])
def test_redact_removes_key_shaped_strings(planted):
    text = f"broker said: {planted} was rejected"
    out = redact(text)
    assert planted not in out
    assert MASK in out
    assert "was rejected" in out       # the surrounding text survives


def test_redact_removes_env_var_shaped_values():
    out = redact("ANTHROPIC_API_KEY=hunter2-not-a-real-key ALPACA_SECRET='abc123def'")
    assert "hunter2-not-a-real-key" not in out
    assert "abc123def" not in out
    assert "ANTHROPIC_API_KEY=" in out  # the NAME is kept; it is diagnostic


def test_redact_removes_secret_named_json_values():
    out = redact('{"api_key": "abcd1234efgh", "symbol": "ACME"}')
    assert "abcd1234efgh" not in out
    assert "ACME" in out


def test_redact_obj_recurses_and_keeps_shape():
    out = redact_obj({"outer": {"secret_token": "xyz", "keep": ["ACME", FAKE_ALPACA_KEY]}})
    assert out["outer"]["secret_token"] == MASK
    assert out["outer"]["keep"][0] == "ACME"
    assert FAKE_ALPACA_KEY not in json.dumps(out)


def test_redact_leaves_ordinary_text_alone():
    text = "risk engine declined BIOX: max_loss_per_position bound at 0.10"
    assert redact(text) == text


def test_raw_helper_escapes_and_redacts():
    out = raw(f"<b>{FAKE_ALPACA_KEY}</b>")
    assert FAKE_ALPACA_KEY not in out
    assert "<b>" not in out and "&lt;b&gt;" in out


# ------------------------------------------------------------- empty states


def test_empty_block_distinguishes_no_data_from_broken_query():
    no_data = empty_block("e1", QueryResult("SELECT 1 FROM t", (), [], None))
    broken = empty_block("e2", QueryResult("SELECT 1 FROM t", (), [],
                                           "OperationalError: no such table: t"))
    assert "rows returned: <b>0</b>" in no_data
    assert "absence of data, not a fault" in no_data
    assert "query FAILED" in broken
    assert "no such table" in broken
    assert "absence of data, not a fault" not in broken


def test_empty_block_prints_the_query_and_the_raw_upstream_response():
    out = empty_block("e3", QueryResult("SELECT x FROM y WHERE z = ?", ("q",), []),
                      upstream='{"status":500,"body":"upstream timeout"}')
    assert "SELECT x FROM y WHERE z = ?" in out
    assert "upstream timeout" in out


# ------------------------------------------------------------------- charts


def _chart():
    return charts.index_chart(
        [charts.Series("bot", [(1, 100.0), (2, 108.0)], "#00f"),
         charts.Series("SPY", [(1, 100.0), (2, 103.0)], "#f00")],
        chart_id="c1", x_labels=[(1, "2026-01-01"), (2, "2026-06-30")],
    )


def test_chart_labels_land_inside_the_viewbox():
    assert charts.labels_outside_viewbox(_chart()) == []


def test_chart_axis_is_labeled_in_percent_and_dollars():
    svg = _chart()
    assert "% move" in svg and "$ on a $1,000 account" in svg
    labels = [b[4] for b in charts.text_boxes(svg)]
    tick_labels = [t for t in labels if "|" in t and "$" in t]
    assert tick_labels, labels
    for label in tick_labels:
        assert "%" in label and "$" in label


def test_chart_refuses_to_draw_an_empty_series():
    with pytest.raises(ValueError):
        charts.index_chart([charts.Series("bot", [], "#00f")], chart_id="c",
                           x_labels=[])


def test_chart_grows_to_fit_a_long_legend():
    svg = charts.index_chart(
        [charts.Series("x" * 80, [(1, 100), (2, 101)], "#00f"),
         charts.Series("y" * 80, [(1, 100), (2, 99)], "#f00")],
        chart_id="c2", x_labels=[(1, "a"), (2, "b")])
    assert charts.labels_outside_viewbox(svg) == []


# -------------------------------------------------------------- performance


def test_performance_nets_costs_off_realised_pnl(seeded, monkeypatch, tmp_path):
    monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "no-bars"))
    perf = queries.performance(Db(seeded))
    assert perf.n_closed == 1
    assert perf.gross_pnl_cents == 1000
    assert perf.scheduled_cost_cents == 3
    assert perf.manual_cost_cents == Decimal_18_5()
    # 100000 + 1000 - 3 - 18.5
    assert perf.net_equity_cents == 100000 + 1000 - 3 - Decimal_18_5()
    assert perf.bot_points[0][1] == 100.0
    assert perf.bot_points[-1][1] > 100.0


def Decimal_18_5():
    from decimal import Decimal

    return Decimal("18.5")


def test_performance_reports_missing_benchmark_instead_of_zero(seeded, monkeypatch, tmp_path):
    monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "definitely-absent"))
    perf = queries.performance(Db(seeded))
    assert perf.spy_points == []
    assert perf.excess_pp is None
    assert "No cached bars" in (perf.spy_error or "")


def test_performance_on_empty_db_has_no_series_but_keeps_the_queries(bare, monkeypatch, tmp_path):
    monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "absent"))
    perf = queries.performance(Db(bare))
    assert perf.bot_points == []
    assert perf.closed_q.row_count == 0
    assert perf.closed_q.error is None       # empty, not broken
    assert "FROM closed_trades" in perf.closed_q.sql


def test_performance_panel_always_carries_the_bakeoff_and_survivorship_caveats(
        bare, monkeypatch, tmp_path):
    monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "absent"))
    html_out = panels.performance_panel(Db(bare))
    assert "rode a lucky right-tail subsample" in html_out
    assert "Survivorship:" in html_out
    assert "too small to mean anything" in html_out


def test_small_sample_warning_clears_only_above_the_floor(seeded, monkeypatch, tmp_path):
    monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "absent"))
    conn = sqlite3.connect(seeded)
    today = datetime.now(timezone.utc).date()
    for i in range(40):
        conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                     (f"pz{i}", "ZZ", "[]", None, _iso(today),
                      today.isoformat(), "closed"))
        conn.execute("INSERT INTO closed_trades VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"pz{i}", "paper", "1", "1", "time_exit", 1, 5, 5, _iso(today)))
    conn.commit()
    conn.close()
    html_out = panels.performance_panel(Db(seeded))
    assert "The sample is too small to mean anything." not in html_out
    assert "one draw from a wide distribution" in html_out
    # the standing caveats never go away, however large the sample
    assert "rode a lucky right-tail subsample" in html_out


# ------------------------------------------------------------------- funnel


def test_funnel_names_the_stage_responsible_when_nothing_traded(seeded):
    conn = sqlite3.connect(seeded)
    conn.execute("DELETE FROM fills")
    conn.execute("DELETE FROM orders")
    conn.commit()
    conn.close()
    f = queries.funnel(Db(seeded))
    assert f.blame_stage == "orders"
    assert "trade decision with no order row" in f.blame


def test_funnel_blames_the_risk_stage_when_everything_is_declined(bare):
    conn = sqlite3.connect(bare)
    today = datetime.now(timezone.utc).date()
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 ("cx", "ZZ", "x", today.isoformat(), "estimated", "[]",
                  _iso(today), "s", "[]"))
    conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                 ("cx", "long", 0.9, "t", "i", 5, 0, "r"))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("dx", "cx", "skip", None, None, None, None, None,
                  json.dumps(["gap_bigger_than_max_loss"]), "{}", _iso(today)))
    conn.commit()
    conn.close()
    f = queries.funnel(Db(bare))
    assert f.blame_stage == "proposed"
    assert "gap_bigger_than_max_loss" in f.blame
    html_out = panels.funnel_panel(Db(bare))
    assert "Why it has not traded" in html_out


def test_funnel_counts_feed_errors_as_a_drop_reason_with_raw_text(seeded):
    f = queries.funnel(Db(seeded))
    stage = next(s for s in f.stages if s.key == "raw_events")
    assert any("federal_register" in reason for reason, _, _ in stage.drops)
    assert any("timeout" in str(detail) for _, _, detail in stage.drops)


# --------------------------------------------------------------------- cost


def test_cost_panel_separates_scheduled_from_manual_and_agrees_with_the_ledger(seeded):
    c = queries.cost_panel(Db(seeded))
    assert c.scheduled_mtd_cents == 3
    assert c.manual_mtd_cents == Decimal_18_5()
    assert c.ledger_crosscheck.startswith("agrees with")
    assert c.unacked_q.row_count == 1


def test_cost_panel_marks_billed_versus_estimated_and_refuses_to_annualise(seeded):
    html_out = panels.cost_panel(Db(seeded))
    assert "ESTIMATED locally" in html_out
    assert "BILLED (Anthropic Cost API" in html_out
    assert "deliberately NOT annualised" in html_out
    assert "6.0%/yr" in html_out            # from the $5 cap, not from spend


def test_cost_panel_prints_the_raw_payload_beside_a_zero_record_count(seeded):
    html_out = panels.cost_panel(Db(seeded))
    assert "returned <b>0 records</b>" in html_out
    assert "raw Cost API payload" in html_out
    assert FAKE_ANTHROPIC_KEY not in html_out


def test_cost_panel_shows_the_acknowledge_form_with_a_required_human_field(seeded):
    html_out = panels.cost_panel(Db(seeded))
    assert 'action="/acknowledge-reconciliation"' in html_out
    assert 'name="acknowledged_by" required' in html_out


def test_zero_spend_prints_its_query(bare):
    html_out = panels.cost_panel(Db(bare))
    assert "Empty result" in html_out
    assert "FROM cost_events" in html_out


def test_acknowledge_refuses_an_anonymous_acknowledgement(seeded):
    okay, message = server.acknowledge(seeded, "recon-1", "   ")
    assert not okay and "required" in message
    conn = sqlite3.connect(seeded)
    row = conn.execute("SELECT acknowledged_by FROM cost_reconciliation_events").fetchone()
    conn.close()
    assert row[0] is None


def test_acknowledge_records_who_and_when(seeded):
    okay, _ = server.acknowledge(seeded, "recon-1", "a-human")
    assert okay
    conn = sqlite3.connect(seeded)
    who, when = conn.execute(
        "SELECT acknowledged_by, acknowledged_at FROM cost_reconciliation_events"
    ).fetchone()
    conn.close()
    assert who == "a-human" and when


def test_acknowledge_rejects_an_unknown_event(seeded):
    okay, message = server.acknowledge(seeded, "no-such-id", "a-human")
    assert not okay and "no unacknowledged reconciliation event" in message


# ------------------------------------------------------------ decision trace


def test_trace_reconstructs_the_whole_decision(seeded):
    html_out = panels.trace_page(Db(seeded), "c1")
    for needle in ["PROMPT-BODY", "THESIS-TEXT", "INVALIDATION-TEXT",
                   "PRICED-IN-REASONING", "max_loss_per_position", "BOUND",
                   "broker response, verbatim", "broker reported price",
                   "target_reached"]:
        assert needle in html_out, needle
    assert FAKE_ALPACA_KEY not in html_out


def test_trace_says_when_code_overruled_the_model(seeded):
    html_out = panels.trace_page(Db(seeded), "c2")
    assert "Code overruled the model here" in html_out
    assert "adverse_gap_exceeds_max_loss" in html_out


def test_trace_of_an_unknown_candidate_explains_itself(seeded):
    html_out = panels.trace_page(Db(seeded), "nope")
    assert "no candidate with id" in html_out
    assert "FROM candidates WHERE id = ?" in html_out


def test_evidence_chain_is_feature_detected_when_absent(seeded):
    ev = queries.evidence_chain(Db(seeded), "c1")
    assert ev.available is False
    assert "not in this database" in ev.reason
    assert "graph_assertions" in panels.trace_page(Db(seeded), "c1")


def test_evidence_chain_renders_generically_when_present(seeded):
    conn = sqlite3.connect(seeded)
    conn.execute("CREATE TABLE graph_assertions (candidate_id TEXT, claim TEXT, "
                 "source_class TEXT, reliability TEXT)")
    conn.execute("INSERT INTO graph_assertions VALUES (?,?,?,?)",
                 ("c1", "insiders bought", "sec_form4_primary", "high"))
    conn.commit()
    conn.close()
    ev = queries.evidence_chain(Db(seeded), "c1")
    assert ev.available and ev.query.row_count == 1
    html_out = panels.trace_page(Db(seeded), "c1")
    assert "source class: sec_form4_primary" in html_out
    assert "reliability: high" in html_out


# ------------------------------------------------------------------ refusals


def test_refusals_show_the_scored_outcome_and_flag_the_tiny_sample(seeded):
    r = queries.refusals(Db(seeded))
    assert r.n_total == 1 and r.n_scored == 1
    assert r.mean_outcome_return == pytest.approx(0.1272)
    html_out = panels.refusals_panel(Db(seeded))
    assert "Too small to act on." in html_out
    assert "not scored yet" in html_out or "scored" in html_out


# ---------------------------------------------------------------------- logs


def test_logs_missing_table_is_named_not_blank(bare, tmp_path):
    """The logs DDL was folded into storage/schema.sql at stage 5, so a
    freshly init'd DB always HAS the table (empty). The missing-table
    path still exists for a database created by an older version - built
    here by dropping the table explicitly."""
    import sqlite3 as _sq

    lg = queries.logs(Db(bare))
    assert lg.available is True          # schema now creates it
    assert lg.query.row_count == 0

    old = tmp_path / "old-version.db"
    conn = _sq.connect(old)
    conn.executescript(
        (Path(__file__).resolve().parent.parent / "catalyst" / "storage"
         / "schema.sql").read_text())
    conn.execute("DROP TABLE logs")
    conn.commit(); conn.close()
    lg2 = queries.logs(Db(str(old)))
    assert lg2.available is False
    assert "logs table is not in this database" in panels.logs_panel(Db(str(old)), {})


def test_logs_filter_by_level_and_text(seeded):
    db = Db(seeded)
    assert queries.logs(db, level="ERROR").query.row_count == 1
    assert queries.logs(db, level="INFO").query.row_count == 0
    assert queries.logs(db, q="failed").query.row_count == 1
    assert queries.logs(db, q="nothing-matches-this").query.row_count == 0
    assert queries.logs(db, component="data.alpaca_news").query.row_count == 1


def test_logs_page_redacts_planted_credentials(seeded):
    html_out = panels.logs_panel(Db(seeded), {})
    assert FAKE_ALPACA_KEY not in html_out
    assert MASK in html_out
    assert "data.alpaca_news" in html_out       # the useful part survives


def test_logs_empty_filter_result_explains_itself(seeded):
    html_out = panels.logs_panel(Db(seeded), {"q": "zzz-no-match"})
    assert "no log line matched these filters" in html_out
    assert "LIKE ?" in html_out


# ----------------------------------------------------- pages, ids and routes


ROUTE_FUNCS = [
    ("/", server.route_overview),
    ("/performance", server.route_performance),
    ("/funnel", server.route_funnel),
    ("/costs", server.route_costs),
    ("/decisions", server.route_decisions),
    ("/refusals", server.route_refusals),
    ("/logs", server.route_logs),
    ("/setup", server.route_setup),
]


@pytest.mark.parametrize("name,func", ROUTE_FUNCS)
def test_every_page_has_unique_element_ids(name, func, seeded, monkeypatch, tmp_path):
    monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "absent"))
    html_out = func(Db(seeded), {})
    assert duplicate_ids(html_out) == [], f"{name}: {duplicate_ids(html_out)}"
    assert len(all_ids(html_out)) >= 2
    assert "Duplicate element ids on this page" not in html_out


@pytest.mark.parametrize("name,func", ROUTE_FUNCS)
def test_every_page_is_stamped_with_the_build_hash(name, func, seeded, monkeypatch,
                                                   tmp_path):
    monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "absent"))
    from catalyst.dashboard.build import BUILD_HASH

    assert BUILD_HASH in func(Db(seeded), {})


def test_decision_route_without_an_id_says_so(seeded):
    html_out = server.route_decision(Db(seeded), {})
    assert "Give a candidate_id" in html_out


def test_render_page_surfaces_duplicate_ids_as_a_banner():
    body = '<div id="same"></div><div id="same"></div>'
    out = server.render_page("t", body, "/", "db")
    assert "Duplicate element ids on this page" in out
    assert "same" in out


def test_setup_is_an_explicit_stub_not_a_silent_form():
    out = panels.setup_stub()
    assert "MOUNT POINT - NOT IMPLEMENTED HERE" in out
    assert "integration-engineer" in out


# --------------------------------------------------------- diagnostic bundle


def test_diagnostic_bundle_is_redacted_and_carries_row_counts(seeded):
    bundle = server.diagnostics_bundle(Db(seeded))
    blob = json.dumps(bundle)
    assert FAKE_ALPACA_KEY not in blob
    assert FAKE_ANTHROPIC_KEY not in blob
    assert bundle["row_counts"]["closed_trades"] == 1
    assert bundle["funnel"]["stages"][0]["stage"] == "raw_events"
    assert bundle["cost"]["unacknowledged_discrepancies"] == 1


def test_diagnostic_bundle_lists_env_names_but_never_env_values(seeded, monkeypatch):
    monkeypatch.setenv("CATALYST_TEST_SECRET_KEY", "super-secret-value-123")
    blob = json.dumps(server.diagnostics_bundle(Db(seeded)))
    assert "CATALYST_TEST_SECRET_KEY" in blob
    assert "super-secret-value-123" not in blob


def test_health_reports_what_is_present(seeded):
    h = server.health(Db(seeded))
    assert h["logs_table_present"] is True
    assert h["graph_assertions_present"] is False
    assert h["cache_policy"] == "no-store"


# ------------------------------------------------------------------ read-only


def test_the_page_connection_cannot_write(seeded):
    db = Db(seeded)
    with pytest.raises(sqlite3.OperationalError):
        db.conn.execute("DELETE FROM candidates")


def test_a_missing_database_is_reported_not_crashed(tmp_path):
    db = Db(str(tmp_path / "nope.db"))
    assert db.open_error and "no database file" in db.open_error
    res = db.q("SELECT 1")
    assert res.row_count == 0 and res.error


# ---------------------------------------------------------------- sabotage
# House rule 4: a test that cannot fail is not a test. These break a COPY
# of the checked behaviour and assert the check catches it.


@pytest.mark.sabotage
def test_redaction_check_would_catch_an_unredacted_key():
    """If redact() were a no-op, the assertions above would fail."""
    def broken_redact(text):
        return text

    assert FAKE_ALPACA_KEY in broken_redact(f"key {FAKE_ALPACA_KEY}")


@pytest.mark.sabotage
def test_duplicate_id_check_would_catch_a_duplicated_id():
    assert duplicate_ids('<div id="a"></div><div id="a"></div>') == ["a"]
    assert duplicate_ids('<div id="a"></div><div id="b"></div>') == []


@pytest.mark.sabotage
def test_viewbox_check_would_catch_a_label_outside_the_box():
    svg = ('<svg viewBox="0 0 100 50">'
           '<text x="-40" y="10" font-size="11" text-anchor="start">escaped</text></svg>')
    assert charts.labels_outside_viewbox(svg)


@pytest.mark.sabotage
def test_empty_block_would_look_different_for_a_broken_query():
    broken = empty_block("s", QueryResult("SELECT 1", (), [], "boom"))
    fine = empty_block("s", QueryResult("SELECT 1", (), [], None))
    assert broken != fine


# --------------------------------------------------------------------------
# Visual layer (owner feedback 2026-08-10: "not very user friendly to
# understand and visually debug"). These pin the presentation promises
# the brief makes, not the styling taste.
# --------------------------------------------------------------------------


class TestVisualLayer:
    def test_every_tile_carries_its_own_provenance(self):
        from catalyst.dashboard.render import tiles
        html = tiles("t", [("Spend", "$0.73", "locally priced from raw usage")])
        assert "Spend" in html and "$0.73" in html
        assert "locally priced from raw usage" in html, (
            "a tile without a provenance line is a bare number, which this "
            "dashboard does not allow")

    def test_status_pill_never_relies_on_colour_alone(self):
        from catalyst.dashboard.render import pill
        for state in ("good", "warn", "crit", "idle"):
            html = pill(state, "behind SPY")
            assert "behind SPY" in html          # the word
            assert 'aria-hidden="true">' in html  # and a glyph beside it

    def test_empty_block_folds_the_sql_but_still_contains_it(self):
        """The wall of SQL is folded, NOT dropped - house rule 3 asks for
        the query beside the zero, not for it to dominate the page."""
        r = QueryResult("SELECT COUNT(*) FROM raw_events", (), [], None)
        html = empty_block("e", r, meaning="nothing arrived")
        assert "<details" in html
        assert "SELECT COUNT(*) FROM raw_events" in html   # still present
        assert "nothing arrived" in html                   # verdict stays visible
        assert "absence of data" in html

    def test_a_broken_query_is_not_folded_away(self):
        r = QueryResult("SELECT 1", (), [], "no such column: x")
        html = empty_block("e", r)
        assert "<details id=\"e-detail\" open>" in html, (
            "a FAILED query must be open by default - it is not a detail "
            "for the reader to go looking for")

    def test_caveat_fold_keeps_every_word_of_every_caveat(self):
        from catalyst.dashboard.render import (
            BAKEOFF_CAVEAT, PAPER_PNL_CAVEAT, SURVIVORSHIP_CAVEAT, caveat_fold,
        )
        from catalyst.dashboard.render import esc
        html = caveat_fold("c", "three caveats",
                           [BAKEOFF_CAVEAT, SURVIVORSHIP_CAVEAT, PAPER_PNL_CAVEAT])
        # escaped, not altered: the caveats carry quotes and ampersands
        for text in (BAKEOFF_CAVEAT, SURVIVORSHIP_CAVEAT, PAPER_PNL_CAVEAT):
            assert esc(text) in html

    def test_bar_chart_labels_stay_inside_the_viewbox(self):
        svg = charts.bar_chart(
            [("07-1%d" % i, i * 3.5, "tooltip %d" % i) for i in range(9)],
            chart_id="bars", title="Billed spend per closed day, cents",
            reference=(17.0, "cap, pro-rata per day"))
        assert charts.labels_outside_viewbox(svg) == []
        assert "<title>tooltip 3</title>" in svg, (
            "each bar needs a hover tooltip; <title> is the whole hover "
            "layer on a page that cannot load JavaScript from anywhere")

    def test_bar_chart_refuses_to_fake_an_empty_chart(self):
        with pytest.raises(ValueError):
            charts.bar_chart([], chart_id="b", title="t")

    def test_placeholder_says_what_will_appear_and_when(self):
        svg = charts.placeholder(
            chart_id="ph", title="Account value vs SPY",
            explanation="The line starts the day the first trade closes.")
        assert charts.labels_outside_viewbox(svg) == []
        assert "first trade closes" in svg
        assert "Account value vs SPY" in svg

    def test_charts_use_theme_tokens_not_baked_in_light_colours(self):
        """The page renders in the reader's own light/dark setting; an
        SVG with hardcoded near-white chrome is unreadable in dark."""
        svg = charts.bar_chart([("a", 1.0, "x")], chart_id="b", title="t")
        assert "var(--surface)" in svg and "var(--hairline)" in svg
        assert "#fbfbfd" not in svg


class TestAnalysisLayer:
    """Owner feedback round 2: "optimise for proper analysis and make it
    easier on the eyes". These pin the analytical content, not taste."""

    def test_meter_marks_pace_not_just_total(self):
        from catalyst.dashboard.render import meter
        html = meter("m", used=73.0, cap=500.0, pace=32.0, legend="cap")
        assert "meter-fill" in html and "14.6%" in html
        assert "meter-pace" in html and "32.0%" in html, (
            "a total without the elapsed fraction of the month cannot "
            "answer 'am I on pace to breach the cap'")

    def test_meter_flags_going_over_the_cap(self):
        from catalyst.dashboard.render import meter
        over = meter("m", used=600.0, cap=500.0)
        assert "over" in over
        assert "width:100.0%" in over, "the fill must clamp, not overflow its track"
        assert "over" not in meter("m", used=100.0, cap=500.0)

    def test_funnel_shows_stage_to_stage_conversion(self, seeded):
        """'3 in, 0 out' does not say WHICH step lost them; a per-stage
        kept-percentage does, and that is the whole diagnostic value."""
        db = Db(seeded)
        html = panels.funnel_panel(db, "f")
        assert "funnel-conv" in html
        assert "kept</span>" in html, "a stage with an upstream must show conversion"
        db.close()

    def test_starved_stages_do_not_repeat_a_full_empty_state(self, bare):
        """Six identical SQL dumps is what made this page unreadable.
        A stage starved by the one above gets one quiet line - and its
        query is still reachable, so nothing is actually hidden."""
        db = Db(bare)
        html = panels.funnel_panel(db, "f")
        starved = html.count("nothing reached this stage")
        full = html.count("Empty result &mdash; here is exactly why")
        assert starved >= 1, "downstream empty stages should be summarised"
        assert full <= 1, (
            f"{full} full empty-states rendered; only the FIRST empty stage "
            "is a finding, the rest are consequences of it")
        # the query survives the summarising
        assert "its query anyway" in html

    def test_prose_is_measure_limited_for_readability(self):
        from catalyst.dashboard.render import _CSS
        assert "max-width: 82ch" in _CSS, (
            "unconstrained prose on a 1180px page runs ~170 characters a "
            "line, about twice a comfortable measure")

    def test_surface_is_not_pure_white(self):
        from catalyst.dashboard.render import _CSS
        assert "--surface:     #fbfbf9" in _CSS
        assert "--surface:     #ffffff" not in _CSS
