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
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
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


# ------------------------------------------------- owner-edited token prices


def test_cost_page_offers_the_token_price_form_with_every_field_required(seeded):
    html_out = panels.cost_panel(Db(seeded))
    assert 'action="/set-token-price"' in html_out
    for field in ("model", "effective_from", "input_cents_per_mtok",
                  "output_cents_per_mtok", "set_by"):
        assert f'name="{field}"' in html_out, field
    # The unit is the whole trap here: cents per million, not dollars.
    assert "CENTS per million tokens" in html_out
    assert "from its date FORWARD" in html_out


def test_the_compact_cost_summary_does_not_carry_the_price_form(seeded):
    """The form is a write path; it belongs on the cost page, not on the
    overview card where it would be clicked by accident."""
    assert 'action="/set-token-price"' not in panels.cost_panel(Db(seeded), compact=True)


def test_the_page_shows_the_rate_actually_in_force_not_the_newest_row(seeded):
    """A rate dated in the future must NOT be shown as today's rate -
    that would silently misstate what the ledger is pricing at."""
    from catalyst.cost.overrides import set_override
    conn = sqlite3.connect(seeded)
    set_override(conn, "claude-sonnet-5", date.today() + timedelta(days=30),
                 "300", "1500", set_by="a-human")
    conn.close()
    html_out = panels.cost_panel(Db(seeded))
    live = html_out.split('id="cost-price-live"')[1].split("</table>")[0]
    row = [r for r in live.split("<tr>") if "claude-sonnet-5<" in r]
    assert len(row) == 1, live
    assert ">200<" in row[0] and ">1000<" in row[0]   # intro rate still in force
    assert ">300<" not in row[0] and "built-in table" in row[0]
    # ...but the future change is visible in the history table.
    hist = html_out.split('id="cost-price-history"')[1].split("</table>")[0]
    assert ">300<" in hist and "a-human" in hist


def test_a_rate_in_force_names_who_set_it(seeded):
    from catalyst.cost.overrides import set_override
    conn = sqlite3.connect(seeded)
    set_override(conn, "claude-opus-5", date.today() - timedelta(days=1),
                 "450", "2200", set_by="Billy")
    conn.close()
    live = panels.cost_panel(Db(seeded)).split('id="cost-price-live"')[1]
    assert "set by Billy" in live
    assert "$4.50" in live          # cents rendered as dollars alongside


def test_never_overridden_says_so_rather_than_showing_an_unexplained_blank(seeded):
    html_out = panels.cost_panel(Db(seeded))
    assert "no rate has ever been overridden by hand" in html_out
    assert "FROM pricing_overrides" in html_out       # the query, printed


def test_set_token_price_endpoint_records_the_rate(seeded):
    okay, message = server.set_token_price(seeded, {
        "model": "claude-sonnet-5", "effective_from": "2026-09-01",
        "input_cents_per_mtok": "300", "output_cents_per_mtok": "1500",
        "set_by": "Billy"})
    assert okay, message
    conn = sqlite3.connect(seeded)
    row = conn.execute(
        "SELECT model, effective_from, input_cents_per_mtok, "
        "output_cents_per_mtok, set_by FROM pricing_overrides").fetchone()
    conn.close()
    assert row == ("claude-sonnet-5", "2026-09-01", "300", "1500", "Billy")


@pytest.mark.parametrize("bad, expect", [
    ({"input_cents_per_mtok": "0"}, "greater than zero"),
    ({"output_cents_per_mtok": "-5"}, "greater than zero"),
    ({"set_by": "  "}, "who made it"),
    ({"model": "gpt-9"}, "No such model"),
    ({"effective_from": "next tuesday"}, "YYYY-MM-DD"),
    ({"input_cents_per_mtok": "three hundred"}, "not a number"),
])
def test_set_token_price_refuses_bad_input_as_a_sentence(seeded, bad, expect):
    form = {"model": "claude-sonnet-5", "effective_from": "2026-09-01",
            "input_cents_per_mtok": "300", "output_cents_per_mtok": "1500",
            "set_by": "Billy"}
    form.update(bad)
    okay, message = server.set_token_price(seeded, form)
    assert not okay
    assert expect in message, message
    assert "Traceback" not in message
    conn = sqlite3.connect(seeded)
    count = conn.execute("SELECT COUNT(*) FROM pricing_overrides").fetchone()[0]
    conn.close()
    assert count == 0, "a refused rate must not reach the table"


def test_typing_dollars_where_cents_are_wanted_is_refused(seeded):
    """3 instead of 300 prices every later call at 1/100th and nothing on
    the dashboard would look wrong. It has to be caught at entry."""
    okay, message = server.set_token_price(seeded, {
        "model": "claude-sonnet-5", "effective_from": "2026-09-01",
        "input_cents_per_mtok": "3", "output_cents_per_mtok": "15",
        "set_by": "Billy"})
    assert not okay
    assert "CENTS per MILLION tokens" in message
    conn = sqlite3.connect(seeded)
    assert conn.execute("SELECT COUNT(*) FROM pricing_overrides").fetchone()[0] == 0
    conn.close()


def test_a_genuinely_large_change_goes_through_when_confirmed(seeded):
    okay, message = server.set_token_price(seeded, {
        "model": "claude-sonnet-5", "effective_from": "2026-09-01",
        "input_cents_per_mtok": "3", "output_cents_per_mtok": "15",
        "set_by": "Billy", "allow_large_change": "1"})
    assert okay, message
    conn = sqlite3.connect(seeded)
    assert conn.execute("SELECT COUNT(*) FROM pricing_overrides").fetchone()[0] == 1
    conn.close()


def test_an_ordinary_rate_change_needs_no_confirmation(seeded):
    """The guard must not obstruct the change it exists to allow. On
    2026-09-01 the built-in rate is 300/1500 (Sonnet 5's intro pricing
    ended the day before), so this is a 20% rise on top of that - the
    shape of every real published rate change."""
    okay, message = server.set_token_price(seeded, {
        "model": "claude-sonnet-5", "effective_from": "2026-09-01",
        "input_cents_per_mtok": "360", "output_cents_per_mtok": "1800",
        "set_by": "Billy"})
    assert okay, message


@pytest.mark.parametrize("effective, baseline", [
    (date(2026, 8, 15), "200"),      # inside Sonnet 5's intro window
    (date(2026, 9, 15), "300"),      # after it ends
])
def test_the_guard_measures_against_the_rate_in_force_on_the_effective_date(
        seeded, effective, baseline):
    """Not against today's rate. The two differ across 2026-08-31, and
    measuring a backdated change against the wrong one would fire the
    guard on a rate that was never in force."""
    from catalyst.cost.overrides import set_override
    conn = sqlite3.connect(seeded)
    with pytest.raises(ValueError) as exc:
        set_override(conn, "claude-sonnet-5", effective, "1", "5",
                     set_by="Billy")
    conn.close()
    assert f"the {baseline} in force" in str(exc.value), str(exc.value)


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
        """Intent, not a specific hex: a pure-white field beside black
        text is the main source of glare. Pinning the exact value made
        this fail on a pure restyle, which is not what it is for."""
        import re as _re
        from catalyst.dashboard.render import _CSS
        surfaces = _re.findall(r"--surface(?:-2)?:\s*(#[0-9a-fA-F]{6})", _CSS)
        assert surfaces, "no surface tokens found at all"
        assert not any(v.lower() == "#ffffff" for v in surfaces), surfaces


class TestOwnerBudgetReconciliation:
    """Owner report: set 20 on the setup page, saw $5 everywhere after.
    The cost page must reconcile the two figures where the owner
    noticed the contradiction, rather than leaving them to disagree."""

    def _panel_with_budget(self, tmp_path, bare, usd):
        from catalyst.setup import credentials as creds
        cpath = str(tmp_path / "c.json")
        creds.save_credentials("PKFAKE1234567890TEST", "SECFAKE",
                               "sk-ant-fake", "tok", path=cpath,
                               settings={"monthly_budget_usd": usd,
                                         "account_mode": "paper"})
        import os
        os.environ["CATALYST_CREDENTIALS"] = cpath
        try:
            db = Db(bare)
            html = panels.cost_panel(db, "cost")
            db.close()
        finally:
            os.environ.pop("CATALYST_CREDENTIALS", None)
        return html

    def test_a_higher_setting_is_enforced_and_prices_its_own_hurdle(
            self, tmp_path, bare):
        """CONTRACT CHANGED at the owner's request: a bigger figure is
        now obeyed. The page must show what that choice costs - a fixed
        bill is a return the strategy has to clear."""
        html = self._panel_with_budget(tmp_path, bare, 20)
        assert "$20/month is the figure being enforced" in html
        assert "above" in html
        assert "24.0% a year" in html, "the hurdle of the choice must be shown"

    def test_a_tighter_setting_is_also_shown_with_its_hurdle(
            self, tmp_path, bare):
        html = self._panel_with_budget(tmp_path, bare, 2)
        assert "$2/month is the figure being enforced" in html
        assert "below" in html
        assert "2.4% a year" in html

    def test_no_message_when_the_setting_matches_the_ceiling(self, tmp_path, bare):
        html = self._panel_with_budget(tmp_path, bare, 5)
        assert "has no effect" not in html
        assert "tighter than" not in html


class TestEnterpriseShell:
    """Owner asked for a professional, navigable terminal. These pin the
    structural promises, not the styling."""

    def test_navigation_is_grouped_not_a_flat_row_of_nine(self):
        from catalyst.dashboard.render import NAV, NAV_GROUPS
        assert len(NAV_GROUPS) >= 3
        assert sum(len(items) for _, items in NAV_GROUPS) == len(NAV)
        for _, items in NAV_GROUPS:
            for href, label, hint in items:
                assert hint, f"{label} has no hint - the group is then just a list"

    def test_active_page_is_marked_for_assistive_tech_too(self):
        out = server.render_page("Cost", "<p>x</p>", "/costs", "db")
        assert 'aria-current="page"' in out
        assert out.count('aria-current="page"') == 1

    def test_shell_has_a_skip_link_and_a_main_landmark(self):
        out = server.render_page("t", "<p>x</p>", "/", "db")
        assert 'class="skip"' in out and 'href="#main"' in out
        assert 'id="main"' in out

    def test_status_rail_states_carry_a_word_and_a_marker(self):
        from catalyst.dashboard.render import status_rail
        html = status_rail([("Account", "$1,000.00", "good"),
                            ("vs S&P", "&mdash;", "idle")])
        assert "Account" in html and "$1,000.00" in html
        assert 'aria-hidden="true"' in html      # marker, not colour alone
        assert 'class="rail-item rail-good"' in html

    def test_the_rail_never_breaks_a_page_when_a_query_fails(
            self, bare, monkeypatch):
        """A missing database does not raise - it yields empty results -
        so force the real failure branch: the rail must say 'unavailable'
        rather than take the whole page down with it."""
        def boom(_db):
            raise RuntimeError("query layer exploded")

        monkeypatch.setattr(queries, "performance", boom)
        db = Db(bare)
        out = server.render_page("t", "<p>x</p>", "/", "db", db=db)
        db.close()
        assert "unavailable" in out            # said, not crashed
        assert "<main" in out

    def test_duplicate_id_banner_survives_a_shell_change(self):
        """The injection used to key off the literal '<main>' and went
        silent the moment the shell grew an id attribute - hiding the
        warning that exists to stop panels failing silently."""
        out = server.render_page("t", '<div id="d"></div><div id="d"></div>',
                                 "/", "db")
        assert "Duplicate element ids on this page" in out


class TestDecisionDossier:
    def test_header_states_the_verdict_before_the_reasoning(self, seeded):
        db = Db(seeded)
        html = panels.trace_page(db, "c1", p="tr")
        db.close()
        assert "Verdict" in html and "Model view" in html
        assert "Size the code chose" in html
        assert "set by the risk engine, never by the model" in html
        # the verdict tiles precede the numbered narrative
        assert html.index("tr-tiles") < html.index("1. What the model was given")

    def test_conviction_is_shown_against_the_floor_it_had_to_clear(self, seeded):
        db = Db(seeded)
        html = panels.trace_page(db, "c1", p="tr")
        db.close()
        assert 'id="tr-conviction"' in html
        assert "gauge-mark" in html, (
            "a conviction with no threshold beside it does not explain "
            "why the trade happened")


class TestEvidenceMindmap:
    def _graph_db(self, tmp_path, rows):
        import uuid as _u
        path = str(tmp_path / "g.db")
        conn = init_db(path)
        conn.executescript(
            open("catalyst/storage/schema_graph.sql").read())
        now = "2026-08-10T12:00:00+00:00"
        conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     ("c1", "GBFH", "insider_cluster", "2026-08-20",
                      "confirmed", "[]", now, "financials", "[]"))
        ents = {"co": ("company", "company:GBFH", "Glen Burnie Bancorp"),
                "ceo": ("person", "person:cik:1", "Nigro, Gerald J"),
                "llc": ("person", "person:cik:2", "Sovereign Holdings LLC")}
        for eid, (kind, key, name) in ents.items():
            conn.execute("INSERT INTO graph_entities VALUES (?,?,?,?,?)",
                         (eid, kind, key, name, now))
        for subj, pred, obj in rows:
            conn.execute(
                "INSERT INTO graph_assertions VALUES (?,?,?,?,?,?,?,?,?)",
                (str(_u.uuid4()), subj, pred, obj, None, "edgar_filing",
                 "acc-1", now, "primary_document"))
        conn.commit(); conn.close()
        return path

    def test_the_company_is_the_centre_even_when_it_is_the_object(
            self, tmp_path):
        """Every 'X bought shares of GBFH' has the company as the
        OBJECT. Reading the object blindly put an LLC in the middle and
        drew the company four times around the rim."""
        path = self._graph_db(tmp_path, [
            ("ceo", "bought shares of", "co"),
            ("llc", "bought shares of", "co"),
        ])
        db = Db(path)
        html = panels.trace_page(db, "c1", p="tr")
        db.close()
        assert 'id="tr-mindmap"' in html
        centre = [m for m in re.findall(r'fill="#ffffff">([^<]+)</text>', html)]
        assert "Glen Burnie Bancorp" in " ".join(centre)
        rim = re.findall(r'fill="var\(--ink-2\)">([^<]+)</text>', html)
        assert any("Nigro" in r for r in rim)
        assert not any("Glen Burnie" in r for r in rim), (
            "the centre must not also be drawn as one of its own branches")

    def test_mindmap_labels_stay_inside_the_viewbox(self):
        svg = charts.mindmap(
            "A very long company name that would overflow a box", [
                ("bought shares of", "Somebody With An Extremely Long Name", 
                 "person", "primary_document", "acc-1"),
                ("dated", "2026-08-05", "event", "official_schedule", "acc-1"),
            ], chart_id="mm")
        assert charts.labels_outside_viewbox(svg) == []

    def test_mindmap_refuses_to_draw_nothing(self):
        with pytest.raises(ValueError):
            charts.mindmap("x", [], chart_id="mm")


class TestTerminalStyling:
    """The Bloomberg-ish restyle, pinned where it carries meaning."""

    @staticmethod
    def _token(name):
        import re as _re
        from catalyst.dashboard.render import _CSS
        return _re.findall(rf"{name}:\s*(#[0-9a-fA-F]{{6}})", _CSS)

    @staticmethod
    def _hue(hex_value):
        import colorsys
        h = hex_value.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        return colorsys.rgb_to_hls(r, g, b)[0] * 360

    def test_chrome_accent_is_not_confusable_with_the_warning_colour(self):
        """The obvious terminal accent is Bloomberg amber - and amber is
        already the reserved WARNING status here. Measured with the
        data-viz validator: amber chrome #ffa028 sits dE 4.5 from
        warning #fab219 in normal vision, far under the 15 floor, so a
        decorative accent would read as an alert and teach the eye to
        ignore alerts. Cyan measures dE 25.0 from warning."""
        accents = self._token("--accent")
        warnings = self._token("--warning")
        assert accents and warnings
        for a in accents:
            for w in warnings:
                gap = abs(self._hue(a) - self._hue(w))
                gap = min(gap, 360 - gap)
                assert gap > 40, (
                    f"accent {a} sits {gap:.0f} degrees from warning {w}; "
                    "a chrome colour that reads as a status is worse than "
                    "a boring chrome colour")

    def test_figures_are_monospaced_so_columns_align(self):
        from catalyst.dashboard.render import _CSS
        assert "ui-monospace" in _CSS
        for selector in (".tile-value", ".rail-value", "td.num"):
            assert selector in _CSS

    def test_the_verbatim_assertions_are_folded_but_still_present(
            self, tmp_path):
        """The mindmap now carries the meaning; the exact wording stays
        one click away rather than dominating the page."""
        import uuid as _u
        path = str(tmp_path / "g.db")
        conn = init_db(path)
        conn.executescript(open("catalyst/storage/schema_graph.sql").read())
        now = "2026-08-10T12:00:00+00:00"
        conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     ("c1", "GBFH", "insider_cluster", "2026-08-20",
                      "confirmed", "[]", now, "financials", "[]"))
        for eid, kind, key, name in [("co", "company", "company:GBFH", "Glen Burnie"),
                                     ("ceo", "person", "person:cik:1", "Nigro")]:
            conn.execute("INSERT INTO graph_entities VALUES (?,?,?,?,?)",
                         (eid, kind, key, name, now))
        conn.execute("INSERT INTO graph_assertions VALUES (?,?,?,?,?,?,?,?,?)",
                     (str(_u.uuid4()), "ceo", "bought shares of", "co", None,
                      "edgar_filing", "acc-1", now, "primary_document"))
        conn.commit(); conn.close()
        db = Db(path)
        html = panels.trace_page(db, "c1", p="tr")
        db.close()
        assert "every assertion behind that diagram, verbatim" in html
        assert "<details" in html
        assert "primary_document" in html      # the data itself survives


class TestValueReconciliation:
    """Owner asked to see the broker's number and ours side by side.
    They differ for two real reasons and the page must name both."""

    @staticmethod
    def _with_broker_read(path, equity="1012.40"):
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT OR REPLACE INTO equity_snapshots VALUES (?,?,?,?,?,?)",
            ("2026-08-10", "2026-08-10T18:00:00+00:00", equity, "800.00",
             "196.40", "broker_read"))
        conn.commit(); conn.close()
        return path

    def test_bridge_names_both_reasons_for_the_gap(self, seeded):
        db = Db(self._with_broker_read(seeded))
        html = panels.value_reconciliation_panel(db, p="val")
        db.close()
        assert "Alpaca account value" in html
        assert "Net value after costs" in html
        assert "not yet banked" in html          # unrealised marks
        assert "API spend to date" in html       # the bill Alpaca cannot see
        assert "a gap is not an error" in html

    def test_it_says_so_when_the_broker_has_never_been_read(self, bare):
        db = Db(bare)
        html = panels.value_reconciliation_panel(db, p="val")
        db.close()
        assert "not read yet" in html
        assert "Empty result" in html            # with its query beside it

    def test_the_two_figures_are_never_presented_as_interchangeable(
            self, seeded):
        db = Db(self._with_broker_read(seeded))
        html = panels.value_reconciliation_panel(db, p="val")
        db.close()
        assert "broker read" in html and "this dashboard" in html, (
            "each figure must be labelled with whose number it is")


# ---------------------------------------- benchmark: too early vs broken


def _perf_with_window(tmp_path, monkeypatch, bar_days, first_activity):
    """A db whose only activity is one cost row on `first_activity`, and
    a SPY cache holding exactly `bar_days`."""
    from catalyst.storage import init_db

    root = tmp_path / "bars"
    root.mkdir(exist_ok=True)
    if bar_days is not None:
        lines = ["date,open,high,low,close,volume"]
        lines += [f"{d},100,101,99,100,1000" for d in bar_days]
        (root / "SPY.csv").write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("CATALYST_BARS", str(root))

    path = str(tmp_path / "p.db")
    conn = init_db(path)
    conn.execute("INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
                 ("c1", "{}", "claude-sonnet-5", "scheduled", "research",
                  "194", f"{first_activity}T14:00:00+00:00", None))
    conn.commit()
    conn.close()
    return Db(path)


def test_a_weekend_old_account_is_not_reported_as_a_broken_benchmark(
        tmp_path, monkeypatch):
    """The owner's case: activity began Sunday 2026-08-09, the cache is
    full and healthy but ends Friday 2026-08-07, so the window holds no
    trading day. That is Tuesday's problem, not a fault."""
    db = _perf_with_window(tmp_path, monkeypatch,
                           ["2026-08-05", "2026-08-06", "2026-08-07"],
                           "2026-08-09")
    perf = queries.performance(db)
    assert perf.spy_points == []
    assert perf.spy_window_too_short is True
    html_out = panels.performance_panel(db, p="perf")
    assert "No SPY comparison yet, and nothing is wrong" in html_out
    assert "younger than one trading day" in html_out
    assert 'id="perf-spy-missing"' not in html_out, (
        "a healthy cache must not raise the missing-benchmark alarm")


def test_a_genuinely_missing_cache_still_raises_the_alarm(tmp_path, monkeypatch):
    """The distinction has to cut both ways, or it is just a softer
    message for every failure."""
    db = _perf_with_window(tmp_path, monkeypatch, None, "2026-08-09")
    perf = queries.performance(db)
    assert perf.spy_window_too_short is False
    html_out = panels.performance_panel(db, p="perf")
    assert 'id="perf-spy-missing"' in html_out
    assert "SPY benchmark unavailable" in html_out


def test_an_empty_cache_file_is_a_fault_not_a_short_window(tmp_path, monkeypatch):
    db = _perf_with_window(tmp_path, monkeypatch, [], "2026-08-09")
    assert queries.performance(db).spy_window_too_short is False


def test_once_a_trading_day_lands_in_the_window_the_comparison_appears(
        tmp_path, monkeypatch):
    db = _perf_with_window(tmp_path, monkeypatch,
                           ["2026-08-05", "2026-08-06", "2026-08-07"],
                           "2026-08-07")
    perf = queries.performance(db)
    assert perf.spy_points, "a bar inside the window must produce a series"
    assert perf.spy_window_too_short is False


def test_the_overview_leads_with_the_broker_value(seeded):
    """Owner request: the account's actual worth at Alpaca is what the
    page is opened for, so it goes above the comparison panels."""
    html_out = server.route_overview(Db(seeded), {})
    assert html_out.index('id="ovval-section"') < html_out.index('id="perf-section"')


# ------------------------------------------- decisions: simple and full


@pytest.fixture
def rich_decision(tmp_path):
    """One fully-populated traded candidate: sources, a model view with
    every field set, a risk decision with every field set, a binding
    limit, and an evidence graph."""
    from catalyst.storage import init_db

    path = str(tmp_path / "rich.db")
    conn = init_db(path)
    conn.executescript(
        (Path(__file__).resolve().parent.parent / "catalyst" / "storage"
         / "schema_graph.sql").read_text())
    now = datetime.now(timezone.utc)
    iso = now.isoformat()
    for src, sid in [("edgar", "acc-1"), ("federal_register", "fr-1")]:
        conn.execute("INSERT INTO raw_events VALUES (?,?,?,?)", (src, sid, iso, "{}"))
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 ("c1", "GBFH", "insider_cluster",
                  (now + timedelta(days=9)).date().isoformat(), "confirmed",
                  json.dumps(["acc-1", "fr-1"]), iso, "financials",
                  json.dumps(["fin"])))
    conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                 ("c1", "long", 0.74, "Cluster of open-market buys.",
                  "A 10b5-1 plan would kill it.", 14, 0, "Not priced in."))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("d1", "c1", "trade", "long", "182.50", "4.1", "39.20",
                  (now + timedelta(days=14)).date().isoformat(), "[]",
                  json.dumps({"conviction_floor": 0.6}), iso))
    conn.execute("INSERT INTO limit_applications VALUES (?,?,?,?,?,?)",
                 ("d1", "max_loss_per_position", "0.12", "0.10", "hard", 1))
    conn.execute("INSERT INTO limit_applications VALUES (?,?,?,?,?,?)",
                 ("d1", "sector_concentration", "0.30", "0.30", "adaptive", 0))
    for eid, kind, key, name in [
            ("e1", "company", "company:GBFH", "GBFH"),
            ("e2", "person", "person:restrepo", "J. Restrepo, CFO"),
            ("e3", "event", "event:q3", "Q3 earnings, 14 Sep")]:
        conn.execute("INSERT INTO graph_entities VALUES (?,?,?,?,?)",
                     (eid, kind, key, name, iso))
    for i, (s_, pred, o_) in enumerate([("e2", "bought shares of", "e1"),
                                        ("e1", "reports on", "e3")]):
        conn.execute("INSERT INTO graph_assertions VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"a{i}", s_, pred, o_, None, "edgar_filing",
                      "SEC Form 4", iso, "primary_document"))
    conn.commit()
    conn.close()
    return path


@pytest.mark.parametrize("must_appear", [
    "GBFH",                     # the candidate
    "edgar",                    # a source it saw
    "federal_register",
    "insider cluster",          # the catalyst type
    "J. Restrepo, CFO",         # an evidence-graph neighbour
    "Q3 earnings, 14 Sep",
    "long",                     # the model's direction
    "conviction 0.74",
    "hold 14 days",             # expected_holding_days
    "not priced in",
    "trade",                    # what the code did
    "$182.50",
    "stop 39.20",
    "max_loss_per_position",    # the limit that BOUND
])
def test_every_recorded_field_reaches_the_spider(rich_decision, must_appear):
    """Each of these is a real column. A wrong column name reads as None
    and drops the fact silently - which is how two of them were missing
    the first time this panel was written."""
    html_out = panels.trace_simple(Db(rich_decision), "c1", p="trs")
    assert must_appear in html_out, f"{must_appear!r} never reached the page"


def test_a_limit_that_did_not_bind_is_not_drawn_as_though_it_did(rich_decision):
    html_out = panels.trace_simple(Db(rich_decision), "c1", p="trs")
    assert "sector_concentration" not in html_out


def test_the_simple_view_leads_with_a_sentence_not_a_table(rich_decision):
    html_out = panels.trace_simple(Db(rich_decision), "c1", p="trs")
    assert "The bot traded GBFH" in html_out
    assert "the risk engine - not the model - chose a size" in html_out
    assert html_out.index("trs-story") < html_out.index("trs-spider")
    assert "<table" not in html_out, "the simple view is the read, not the record"


def test_the_spider_groups_the_three_stages_of_the_decision(rich_decision):
    html_out = panels.trace_simple(Db(rich_decision), "c1", p="trs")
    for arm in ("What it saw", "What it concluded", "What the code did"):
        assert arm in html_out, arm


def test_identity_is_never_colour_alone(rich_decision):
    """A light-mode slot is under 3:1 on the surface, so the relief rule
    applies: every arm is named in text as well as coloured."""
    html_out = panels.trace_simple(Db(rich_decision), "c1", p="trs")
    svg = html_out[html_out.index("<svg"):html_out.index("</svg>")]
    for arm in ("What it saw", "What it concluded", "What the code did"):
        assert arm in svg, f"{arm} is not labelled inside the diagram"


def test_both_views_are_reachable_from_each_other(rich_decision):
    simple = panels.trace_simple(Db(rich_decision), "c1", p="trs")
    full = panels.trace_page(Db(rich_decision), "c1", p="tr")
    assert "view=full" in simple
    assert "view=simple" in full


def test_the_decision_route_defaults_to_simple(rich_decision):
    db = Db(rich_decision)
    assert 'id="trs-spider"' in server.route_decision(db, {"candidate_id": ["c1"]})
    full = server.route_decision(db, {"candidate_id": ["c1"], "view": ["full"]})
    assert 'id="trs-spider"' not in full
    assert "1. What the model saw" in full or 'id="tr-tiles"' in full


def test_a_candidate_with_nothing_recorded_says_so(bare, tmp_path):
    from catalyst.storage import init_db
    path = str(tmp_path / "thin.db")
    conn = init_db(path)
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 ("c9", "NUL", "unknown", "2026-09-01", "estimated", "[]",
                  _iso(date.today()), "unknown", "[]"))
    conn.commit()
    conn.close()
    html_out = panels.trace_simple(Db(path), "c9", p="trs")
    # The catalyst type alone is still something, so the diagram draws;
    # what must never happen is a crash or a blank panel.
    assert "trs-section" in html_out
    assert "NUL" in html_out


# --------------------------------------------------------- the brain map


@pytest.fixture
def wired(tmp_path):
    """A database with a real chain: two sources feed a candidate, the
    candidate carries an evidence graph, a model view, a risk decision
    and an outcome."""
    from catalyst.storage import init_db

    path = str(tmp_path / "wired.db")
    conn = init_db(path)
    conn.executescript(
        (Path(__file__).resolve().parent.parent / "catalyst" / "storage"
         / "schema_graph.sql").read_text())
    iso = datetime.now(timezone.utc).isoformat()
    for src, sid in [("edgar", "e-1"), ("federal_register", "f-1")]:
        conn.execute("INSERT INTO raw_events VALUES (?,?,?,?)", (src, sid, iso, "{}"))
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 ("c1", "GBFH", "insider_cluster", "2026-09-01", "confirmed",
                  # "ghost-1" is named by the candidate but was never
                  # stored as a raw event - it must produce NO node and
                  # NO edge, or the map claims a source the bot cannot
                  # show you.
                  json.dumps(["e-1", "f-1", "ghost-1"]), iso, "financials", "[]"))
    conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                 ("c1", "long", 0.74, "t", "i", 14, 0, "r"))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("d1", "c1", "trade", "long", "180.00", "4.0", "39.00",
                  "2026-09-15", "[]", "{}", iso))
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                 ("p1", "GBFH", "[]", "s1", iso, "2026-09-15", "closed"))
    conn.execute("INSERT INTO closed_trades VALUES (?,?,?,?,?,?,?,?,?)",
                 ("p1", "paper", "42.71", "45.02", "target_reached",
                  1000, 12, 4, iso))
    for eid, kind, key, name in [("e1", "company", "company:GBFH", "GBFH"),
                                 ("e2", "person", "person:r", "J. Restrepo, CFO")]:
        conn.execute("INSERT INTO graph_entities VALUES (?,?,?,?,?)",
                     (eid, kind, key, name, iso))
    conn.execute("INSERT INTO graph_assertions VALUES (?,?,?,?,?,?,?,?,?)",
                 ("a1", "e2", "bought shares of", "e1", None, "edgar_filing",
                  "SEC Form 4", iso, "primary_document"))
    conn.commit()
    conn.close()
    return path


def test_the_brain_draws_the_whole_chain(wired):
    b = queries.brain(Db(wired))
    names = [label for label, _ in b.layers]
    assert names == ["Sources", "Candidates", "What it linked", "Model view",
                     "Risk engine", "Outcome"]
    assert b.edge_count >= 5
    html_out = panels.brain_panel(Db(wired), p="brain")
    for expect in ("edgar", "federal register", "GBFH", "J. Restrepo, CFO",
                   "long", "trade", "target reached"):
        assert expect in html_out, expect


def test_an_entity_is_labelled_by_its_NAME_not_its_key(wired):
    """The key is "person:r"; the name is "J. Restrepo, CFO". Drawing the
    key shows an id where a name belongs, which is not evidence anyone
    can check."""
    html_out = panels.brain_panel(Db(wired), p="brain")
    svg = html_out[html_out.index("<svg"):html_out.index("</svg>")]
    assert "J. Restrepo, CFO" in svg
    assert ">person:r<" not in svg


def test_every_drawn_edge_has_both_endpoints_on_the_map(wired):
    """An edge to a node that was never drawn would be a line into empty
    space - and worse, a link the reader cannot check."""
    b = queries.brain(Db(wired))
    drawn = {nid for _, nodes in b.layers for nid, _, _ in nodes}
    for src, dst, _, _ in b.edges:
        assert src in drawn or dst in drawn, f"{src}->{dst} touches no node"


def test_the_brain_invents_no_links(wired):
    """Every edge must trace to a row. The exact count is checked because
    a layout that 'looks denser' by adding connectors is the one failure
    this picture must never have."""
    b = queries.brain(Db(wired))
    sources = [s for s, _, _, _ in b.edges if s.startswith("src:")]
    assert len(sources) == 2, (
        "one edge per source event that was actually STORED - the "
        f"candidate names three, one of which does not exist: {sources}")
    assert not any("ghost" in s for s in sources)
    views = [e for e in b.edges if e[1].startswith("view:")]
    assert len(views) == 1
    outcomes = [e for e in b.edges if e[1].startswith("out:")]
    assert len(outcomes) == 1


def test_an_empty_database_says_so_and_prints_its_queries(bare):
    html_out = panels.brain_panel(Db(bare), p="brain")
    assert "Nothing is wired up yet" in html_out
    assert "FROM candidates" in html_out, "the query behind the emptiness"
    assert "<svg" not in html_out, "nothing to draw means nothing drawn"


def test_the_brain_route_is_in_the_nav_and_renders(wired):
    from catalyst.dashboard.render import NAV
    assert any(href == "/brain" for href, _ in NAV)
    assert 'id="brain-map"' in server.route_brain(Db(wired), {})


# ------------------------------------------------------ refusals, simple


def test_refusals_simple_maps_reason_to_candidate_to_outcome(seeded):
    html_out = panels.refusals_simple(Db(seeded), p="refs")
    assert 'id="refs-map"' in html_out
    svg = html_out[html_out.index("<svg"):html_out.index("</svg>")]
    assert "adverse gap exceeds max loss" in svg   # the reason
    assert "BIOX" in svg                            # the candidate
    assert "went UP after refusal" in svg           # what it then did


def test_an_unscored_refusal_is_never_counted_as_an_outcome(seeded, tmp_path):
    """Scoring is the whole point of the tracker; an unscored refusal
    must read as unfinished business, not as a result."""
    conn = sqlite3.connect(seeded)
    conn.execute("UPDATE refusals SET scored_at = NULL, outcome_return = NULL")
    conn.commit()
    conn.close()
    html_out = panels.refusals_simple(Db(seeded), p="refs")
    svg = html_out[html_out.index("<svg"):html_out.index("</svg>")]
    assert "not scored yet" in svg
    assert "went UP after refusal" not in svg, (
        "an unscored refusal was drawn as though its outcome were known")
    assert "none scored yet" in html_out


def test_both_refusal_views_reach_each_other(seeded):
    simple = panels.refusals_simple(Db(seeded), p="refs")
    full = panels.refusals_panel(Db(seeded), p="ref")
    assert "view=full" in simple and "view=simple" in full


def test_the_refusals_route_defaults_to_the_map(seeded):
    db = Db(seeded)
    assert 'id="refs-map"' in server.route_refusals(db, {})
    assert 'id="ref-table"' in server.route_refusals(db, {"view": ["full"]})
