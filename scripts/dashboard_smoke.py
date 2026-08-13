#!/usr/bin/env python3
"""Render the dashboard for real and MEASURE the output.

Run manually (not under pytest - it binds a local socket, and the test
suite is fully offline by contract):

    python3 scripts/dashboard_smoke.py

It seeds two throwaway databases, serves each on an ephemeral 127.0.0.1
port, fetches every route with urllib, and asserts on the actual HTML:

  * the bake-off caveat, the survivorship caveat and the small-sample
    warning are present on the performance panel;
  * the funnel names the stage responsible when nothing has traded;
  * a planted FAKE credential is gone from the logs page, the decision
    trace and the diagnostic bundle;
  * element ids are unique on every page;
  * chart labels land inside the chart's own viewBox (measured from the
    rendered SVG, not read from the code);
  * an empty equity series prints its raw query and row count;
  * the acknowledge endpoint refuses an anonymous acknowledgement and
    accepts a named one, and the write actually lands.

Nothing here talks to the network beyond localhost.
"""

import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalyst.dashboard import charts  # noqa: E402
from catalyst.dashboard.build import BUILD_HASH  # noqa: E402
from catalyst.dashboard.render import all_ids, duplicate_ids  # noqa: E402
from catalyst.dashboard.server import make_server  # noqa: E402
from catalyst.storage import init_db  # noqa: E402

# A FAKE credential shape, planted on purpose. Never a real key.
FAKE_ALPACA_KEY = "PKFAKE123456789TEST"
FAKE_ANTHROPIC_KEY = "sk-ant-FAKE-0000000000000000"

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        PASS.append(name)
        print(f"  ok   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}\n       {detail}")


def _iso(d: date) -> str:
    return datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc).isoformat()


def seed_traded_db(path: str) -> None:
    """A full decision trace end to end, plus a cost ledger with one
    unacknowledged discrepancy."""
    conn = init_db(path)
    conn.executescript(
        (Path(__file__).resolve().parent.parent
         / "catalyst" / "dashboard" / "schema_logs.sql").read_text()
    )
    today = datetime.now(timezone.utc).date()
    d7 = today - timedelta(days=7)
    d5 = today - timedelta(days=5)
    d1 = today - timedelta(days=1)

    conn.execute(
        "INSERT INTO raw_events VALUES (?,?,?,?)",
        ("sec_insider", "0001234567-26-000042", _iso(d7),
         json.dumps({"accession": "0001234567-26-000042", "issuer": "ACME CORP",
                     "form": "4", "transaction_code": "P", "shares": 12000})),
    )
    conn.execute(
        "INSERT INTO raw_events_errors VALUES (?,?,?)",
        ("federal_register", _iso(d7),
         '{"status":500,"body":"upstream timeout after 30s"}'),
    )
    conn.execute(
        "INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
        ("cand-traded-1", "ACME", "insider_cluster", d1.isoformat(), "confirmed",
         json.dumps(["0001234567-26-000042"]), _iso(d7), "industrials",
         json.dumps(["industrials", "insider_cluster", f"week-{d1.isocalendar()[1]}"])),
    )
    conn.execute(
        "INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
        ("call-1", "cand-traded-1", "claude-haiku-4-5",
         "You are researching ACME. Cluster of 3 insider purchases filed "
         f"{d7}. Question: is this already priced in?",
         json.dumps(["web_search", "submit_research_view"]), "3.4210", 8123, None,
         _iso(d7)),
    )
    conn.execute(
        "INSERT INTO research_call_turns VALUES (?,?,?,?,?)",
        ("call-1", 0,
         json.dumps({"id": "msg_01", "stop_reason": "tool_use",
                     "content": [{"type": "text", "text": "Searching filings."}]}),
         json.dumps({"input_tokens": 4211, "output_tokens": 233,
                     "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1024,
                     "server_tool_use": {"web_search_requests": 1}}),
         "tool_use"),
    )
    conn.execute(
        "INSERT INTO research_call_turns VALUES (?,?,?,?,?)",
        ("call-1", 1,
         json.dumps({"id": "msg_02", "stop_reason": "tool_use",
                     "content": [{"type": "tool_use", "name": "submit_research_view",
                                  "input": {"direction": "long", "conviction": 0.71}}]}),
         json.dumps({"input_tokens": 5300, "output_tokens": 190,
                     "cache_creation_input_tokens": 0, "cache_read_input_tokens": 4096}),
         "tool_use"),
    )
    conn.execute(
        "INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
        ("cand-traded-1", "long", 0.71,
         "Three officers bought $180k combined on the open market within 8 days, "
         "the first cluster since 2024, and the tape has not moved.",
         "Any of the three filing a 10b5-1 amendment, or the stock gapping "
         "more than 8% before entry.",
         12, 0,
         "Volume on the filing day was 0.9x its 20-day median and the close was "
         "flat; a priced-in cluster shows a same-day move."),
    )
    conn.execute(
        "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("dec-1", "cand-traded-1", "trade", "long", "196.40", "4.6", "41.80",
         (d1 + timedelta(days=12)).isoformat(), json.dumps([]),
         json.dumps({"conviction_floor": 0.60, "stop_width": 0.12,
                     "adverse_gap_assumption": {"insider_cluster": 0.18}}),
         _iso(d5)),
    )
    for rule, requested, bound, kind, binding in [
        ("max_loss_per_position", "0.12", "0.10", "hard", 1),
        ("max_total_exposure", "0.44", "0.60", "hard", 0),
        ("max_spread_bp", "9.4", "20.0", "adaptive", 0),
    ]:
        conn.execute("INSERT INTO limit_applications VALUES (?,?,?,?,?,?)",
                     ("dec-1", rule, bound, requested, kind, binding))
    conn.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
        # orders.decision_id holds the CANDIDATE id - that is what
        # execution/orders.py writes (decision.candidate_id) and what the
        # foreign key points at. Seeding "dec-1" here made this whole
        # script die on a FOREIGN KEY constraint, so nothing had been
        # rendered for real in a while.
        ("ord-1", "cand-traded-1", "b0a1-broker-id", "buy", "4.6", "market", "day",
         _iso(d5), "filled",
         json.dumps({"id": "b0a1-broker-id", "status": "filled",
                     "filled_avg_price": "42.71",
                     "request_headers_echo": {"APCA-API-KEY-ID": FAKE_ALPACA_KEY}})),
    )
    conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?)",
                 ("ord-1", "42.71", "4.6", _iso(d5), "42.71", "0.064"))
    conn.execute(
        "INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
        ("pos-1", "ACME", json.dumps(["ord-1"]), "ord-stop-1", _iso(d5),
         (d1 + timedelta(days=12)).isoformat(), "closed"),
    )
    conn.execute("INSERT INTO stop_confirmations VALUES (?,?,?,?)",
                 ("pos-1", _iso(d1), json.dumps(["ord-stop-1"]), "ok"))
    # Two reviews: one that changed nothing and one that acted. The
    # HOLD is the important one to render - a dossier showing only the
    # reviews that closed something makes the model look decisive in
    # hindsight and hides the far more common answer.
    conn.execute(
        "INSERT INTO position_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("rev-1", "pos-1", "ACME", "hold", 0,
         "the phase 3 readout has not reported; nothing has changed",
         json.dumps([]), "REVIEWING AN OPEN POSITION...", None,
         "claude-sonnet-5", "7.9", None, _iso(d1 + timedelta(days=2))))
    conn.execute(
        "INSERT INTO position_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("rev-2", "pos-1", "ACME", "exit_now", 1,
         "the readout missed its primary endpoint",
         json.dumps(["phase 3 missed", "two analysts cut to hold"]),
         "REVIEWING AN OPEN POSITION...", None, "claude-sonnet-5", "8.2",
         None, _iso(d1 + timedelta(days=4))))
    conn.execute(
        "INSERT INTO closed_trades VALUES (?,?,?,?,?,?,?,?,?)",
        ("pos-1", "paper", "42.71", "45.02", "target_reached", 1063, 12, 4, _iso(d1)),
    )

    # A second, declined candidate with a scored refusal.
    conn.execute(
        "INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
        ("cand-declined-1", "BIOX", "clinical_readout", (today + timedelta(days=9)).isoformat(),
         "estimated", json.dumps(["nct-0099"]), _iso(d5), "healthcare",
         json.dumps(["healthcare", "clinical_readout"])),
    )
    conn.execute(
        "INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
        ("call-2", "cand-declined-1", "claude-haiku-4-5",
         "You are researching BIOX ahead of a phase 3 readout.",
         json.dumps(["submit_research_view"]), "2.1000", 6400, None, _iso(d5)),
    )
    conn.execute(
        "INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
        ("cand-declined-1", "long", 0.68,
         "Readout is binary and the market has not repriced.",
         "A pre-announcement or an offering.", 10, 0,
         "Implied move is 40% and the shares have drifted sideways."),
    )
    conn.execute(
        "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("dec-2", "cand-declined-1", "skip", None, None, None, None, None,
         json.dumps(["adverse_gap_assumption_exceeds_max_loss_per_position",
                     "sector_cluster_would_exceed_max_correlated_cluster_pct"]),
         json.dumps({"conviction_floor": 0.60,
                     "adverse_gap_assumption": {"clinical_readout": 0.60}}),
         _iso(d5)),
    )
    conn.execute("INSERT INTO limit_applications VALUES (?,?,?,?,?,?)",
                 ("dec-2", "max_loss_per_position", "0.10", "0.60", "hard", 1))
    conn.execute("INSERT INTO refusals VALUES (?,?,?,?,?,?,?)",
                 ("dec-2", "cand-declined-1", "11.40", _iso(d5), _iso(d1),
                  "12.85", "0.1272"))

    # Cost ledger: scheduled + manual, one unacknowledged discrepancy.
    for i, (kind, cents, component) in enumerate([
        ("scheduled", "3.4210", "research"),
        ("scheduled", "2.1000", "research"),
        ("manual", "18.5000", "backtest_judgement"),
    ]):
        conn.execute(
            "INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
            (f"ce-{i}", json.dumps({"input_tokens": 4211, "output_tokens": 233,
                                    "cache_read_input_tokens": 1024,
                                    "server_tool_use": {"web_search_requests": 1}}),
             "claude-haiku-4-5", kind, component, cents, _iso(d1), f"call-{i}"),
        )
    conn.execute(
        "INSERT INTO cost_reconciliation_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("recon-1", d1.isoformat(), "all", json.dumps({"scheduled": "5.52"}),
         "5.5210", "0", "5.5210", "5",
         json.dumps({"data": [], "has_more": False,
                     "request": {"x-api-key": FAKE_ANTHROPIC_KEY}}),
         0, "scheduled_paused", None, None, _iso(d1)),
    )
    conn.execute(
        "INSERT INTO cost_governor_events VALUES (?,?,?,?,?,?,?)",
        ("cycle-9", "scheduled", "4.0", "500", "deny",
         "reconciliation_discrepancy_unacknowledged", _iso(today)),
    )
    conn.execute(
        "INSERT INTO adaptive_param_log VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("conviction_floor", "0.60", "0.63", json.dumps(["pos-1"]),
         _iso(d7), _iso(d1), "1 closed trade - below MIN_SAMPLE_SIZE, logged as a "
         "proposal only", _iso(d1), "0.60", None),
    )
    conn.execute(
        "INSERT INTO logs (ts, level, component, message, cycle_id, candidate_id, "
        "traceback_text, context_json) VALUES (?,?,?,?,?,?,?,?)",
        (_iso(d1), "ERROR", "data.alpaca_news",
         f"news fetch failed using key {FAKE_ALPACA_KEY}", "cycle-9", "cand-traded-1",
         "Traceback (most recent call last):\n  ConnectionError: reset",
         json.dumps({"ALPACA_API_KEY": FAKE_ALPACA_KEY, "symbols": ["ACME"]})),
    )
    conn.execute(
        "INSERT INTO logs (ts, level, component, message, cycle_id, candidate_id, "
        "traceback_text, context_json) VALUES (?,?,?,?,?,?,?,?)",
        (_iso(d5), "INFO", "risk.evaluate", "declined BIOX: hard bound bound first",
         "cycle-7", "cand-declined-1", None, None),
    )
    conn.commit()
    conn.close()


def seed_empty_db(path: str) -> None:
    """Candidates exist, research ran, risk declined everything, nothing
    traded: the case where the funnel must name the stage responsible.
    Also the empty-equity case - no closed trades, no cost rows."""
    conn = init_db(path)
    today = datetime.now(timezone.utc).date()
    conn.execute("INSERT INTO raw_events VALUES (?,?,?,?)",
                 ("clinicaltrials", "NCT00001", _iso(today), '{"nctId":"NCT00001"}'))
    conn.execute(
        "INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
        ("cand-x", "ZZZZ", "clinical_readout", today.isoformat(), "estimated",
         json.dumps(["NCT00001"]), _iso(today), "healthcare", json.dumps(["healthcare"])),
    )
    conn.execute(
        "INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
        ("call-x", "cand-x", "claude-haiku-4-5", "prompt", json.dumps([]),
         "1.0", 100, None, _iso(today)),
    )
    conn.execute(
        "INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
        ("cand-x", "long", 0.8, "thesis", "invalidation", 10, 0, "reasoning"),
    )
    conn.execute(
        "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("dec-x", "cand-x", "skip", None, None, None, None, None,
         json.dumps(["adverse_gap_assumption_exceeds_max_loss_per_position"]),
         json.dumps({}), _iso(today)),
    )
    conn.execute("INSERT INTO limit_applications VALUES (?,?,?,?,?,?)",
                 ("dec-x", "max_loss_per_position", "0.10", "0.60", "hard", 1))
    conn.commit()
    conn.close()


def write_spy_cache(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    lines = ["date,open,high,low,close,volume"]
    price = 600.0
    for i in range(20):
        day = today - timedelta(days=19 - i)
        price *= 1.001
        lines.append(f"{day},{price:.2f},{price:.2f},{price:.2f},{price:.2f},1000000")
    (root / "SPY.csv").write_text("\n".join(lines) + "\n")
    (root / "cache_meta.json").write_text(json.dumps({"fetched_at": str(today)}))


class Served:
    def __init__(self, db_file: str):
        self.httpd = make_server("127.0.0.1", 0, db_file)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8")

    def post(self, path: str, data: dict, follow=False):
        url = f"http://127.0.0.1:{self.port}{path}"
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=body, method="POST")

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None

        opener = (urllib.request.build_opener()
                  if follow else urllib.request.build_opener(_NoRedirect))
        try:
            with opener.open(req, timeout=20) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8")


import urllib.parse  # noqa: E402  (used by Served.post)


TRACE_SIMPLE = "/decision?candidate_id=cand-traded-1"
TRACE_FULL = TRACE_SIMPLE + "&view=full"

ROUTES = ["/", "/performance", "/funnel", "/costs", "/decisions", "/brain",
          TRACE_SIMPLE, TRACE_FULL,
          "/decision?candidate_id=cand-declined-1",
          "/decision?candidate_id=cand-declined-1&view=full",
          "/refusals", "/logs", "/logs?level=ERROR&q=news",
          "/maintenance", "/setup",
          "/health", "/diagnostics.json"]


def phase_traded(tmp: Path) -> None:
    print("\n[phase 1] populated database, SPY cache present")
    db_file = str(tmp / "traded.db")
    seed_traded_db(db_file)
    bars = tmp / "bars"
    write_spy_cache(bars)
    os.environ["CATALYST_BARS"] = str(bars)

    with Served(db_file) as s:
        pages = {}
        for route in ROUTES:
            status, headers, body = s.get(route)
            pages[route] = body
            check(f"GET {route} -> 200", status == 200, f"got {status}")
            check(f"{route} is uncached",
                  headers.get("Cache-Control", "").startswith("no-store"),
                  f"Cache-Control={headers.get('Cache-Control')!r}")
            check(f"{route} carries the build hash",
                  headers.get("X-Catalyst-Build") == BUILD_HASH,
                  f"header={headers.get('X-Catalyst-Build')!r} vs {BUILD_HASH}")

        health = json.loads(pages["/health"])
        check("/health build hash matches the served pages",
              health["build_hash"] == BUILD_HASH)
        check("footer stamps the build hash",
              BUILD_HASH in pages["/performance"])

        # --- unique ids on every HTML page ---
        for route, body in pages.items():
            if not body.lstrip().startswith("<!doctype"):
                continue
            dupes = duplicate_ids(body)
            check(f"{route} has unique element ids",
                  not dupes, f"duplicates: {dupes}")
            check(f"{route} shows no duplicate-id banner",
                  "Duplicate element ids on this page" not in body)
            check(f"{route} actually has ids to check", len(all_ids(body)) >= 2,
                  f"only {len(all_ids(body))} ids found - the uniqueness check "
                  f"would be vacuous")

        perf = pages["/performance"]
        check("performance panel carries the bake-off subsample-luck caveat",
              "rode a lucky right-tail subsample" in perf and "-15.17pp" in perf)
        check("performance panel carries the survivorship statement",
              "Survivorship:" in perf and "delisting-complete" in perf)
        check("performance panel says the sample is too small to mean anything",
              "The sample is too small to mean anything." in perf)
        check("performance panel states the excess return as a number",
              "excess return against SPY" in perf)
        check("chart axis is labeled in both % and $",
              "% move" in perf and "$ on a $1,000 account" in perf)

        # --- measure the rendered SVG, do not trust the code ---
        start = perf.index("<svg")
        end = perf.index("</svg>") + len("</svg>")
        svg = perf[start:end]
        outside = charts.labels_outside_viewbox(svg)
        check("every chart label lands inside the chart viewBox",
              not outside, f"outside: {outside}")
        check("chart plots both series",
              svg.count("<polyline") == 2, f"{svg.count('<polyline')} polylines")

        # --- redaction ---
        for route in ["/logs", "/decision?candidate_id=cand-traded-1",
                      "/diagnostics.json", "/costs"]:
            body = pages.get(route) or s.get(route)[2]
            check(f"planted FAKE Alpaca key is redacted on {route}",
                  FAKE_ALPACA_KEY not in body)
            check(f"planted FAKE Anthropic key is redacted on {route}",
                  FAKE_ANTHROPIC_KEY not in body)
        check("redaction leaves a visible marker rather than dropping text",
              "[REDACTED]" in pages["/logs"])
        bundle = json.loads(pages["/diagnostics.json"])
        check("diagnostic bundle carries row counts",
              bundle["row_counts"].get("closed_trades") == 1,
              str(bundle["row_counts"])[:200])
        check("diagnostic bundle lists env var NAMES only",
              "env_var_names_only" in bundle
              and all(isinstance(x, str) for x in bundle["env_var_names_only"]))

        # --- decision trace completeness ---
        # The audit trail lives on the FULL view; the simple view is the
        # one-picture summary and must link to it. Checking the simple
        # view for the prompt (as this did) reported the whole trail
        # missing when it was one click away.
        simple = pages[TRACE_SIMPLE]
        check("simple view links to the full record",
              "view=full" in simple)
        trace = pages[TRACE_FULL]
        for needle, label in [
            ("the exact prompt sent", "prompt the model saw"),
            ("verbatim API response and usage", "raw API turns"),
            ("Three officers bought", "the model's thesis verbatim"),
            ("what would invalidate it", "invalidation verbatim"),
            ("max_loss_per_position", "risk limit rows"),
            ("BOUND", "binding flag on the limit that bound"),
            ("broker response, verbatim", "broker raw response"),
            ("broker reported price", "fill vs modeled price"),
            ("target_reached", "exit trigger"),
            ("Was it still worth holding?", "the position-review section"),
            ("the readout missed its primary endpoint", "a review that ACTED"),
            ("nothing has changed", "a review that changed nothing"),
            ("two analysts cut to hold", "what changed since entry"),
            ("never push it", "the only-ever-shorten rule, stated on screen"),
        ]:
            check(f"trace shows {label}", needle in trace)
        check("trace feature-detects the evidence graph rather than assuming it",
              "graph_assertions" in trace)

        declined = pages["/decision?candidate_id=cand-declined-1&view=full"]
        check("declined trace says code overruled the model",
              "Code overruled the model here" in declined)

        # --- refusals ---
        ref = pages["/refusals"]
        check("refusals page shows the scored outcome",
              "0.1272" in ref and "mean outcome return" in ref)
        check("refusals page refuses to treat a tiny sample as evidence",
              "Too small to act on." in ref)

        # --- cost provenance and the acknowledge write path ---
        costs = pages["/costs"]
        check("cost numbers say billed vs estimated",
              "ESTIMATED locally" in costs and "BILLED (Anthropic Cost API" in costs)
        check("cost panel separates scheduled from manual",
              "scheduled (runtime) spend" in costs
              and "manual (build/testing) spend" in costs)
        check("cost panel shows the lifetime build budget against $200",
              "lifetime build budget used" in costs and "$200.00" in costs)
        check("cost panel refuses to annualise a partial month",
              "deliberately NOT annualised" in costs)
        check("cost panel states the hurdle from the cap",
              "6.0%/yr" in costs)
        check("unacknowledged discrepancy is loud",
              "unacknowledged reconciliation discrepancy" in costs.lower()
              or "unacknowledged" in costs)
        check("a zero API record count prints the raw payload beside it",
              "returned <b>0 records</b>" in costs)

        status, _, body = s.post("/acknowledge-reconciliation",
                                 {"event_id": "recon-1", "acknowledged_by": ""})
        check("anonymous acknowledgement is refused", status == 400, f"got {status}")
        check("refusal explains why",
              "acknowledged_by is required" in body)

        status, headers, _ = s.post("/acknowledge-reconciliation",
                                    {"event_id": "recon-1",
                                     "acknowledged_by": "smoke-test-human"})
        check("named acknowledgement is accepted", status == 303, f"got {status}")
        conn = sqlite3.connect(db_file)
        row = conn.execute("SELECT acknowledged_by, acknowledged_at FROM "
                           "cost_reconciliation_events WHERE id='recon-1'").fetchone()
        conn.close()
        check("the acknowledgement actually landed in the database",
              row[0] == "smoke-test-human" and row[1], str(row))
        after = s.get("/costs")[2]
        check("cost page reflects the acknowledgement",
              "No unacknowledged reconciliation discrepancies" in after)

        status, _, body = s.post("/setup", {"anything": "1"})
        check("setup POST is an honest 501 stub", status == 501, f"got {status}")
        check("setup stub says who mounts the real thing",
              "catalyst.orchestrator.scheduler" in body, body[:300])
        check("a POST the setup app declines does not hang the connection",
              status == 501,
              "do_POST used to read the request body twice, blocking the "
              "request thread on an empty socket until the client gave up")
        check("setup GET marks the mount point",
              "MOUNT POINT - NOT IMPLEMENTED HERE" in pages["/setup"])

        status, _, _ = s.get("/no-such-route")
        check("unknown route is a 404, not a crash", status == 404, f"got {status}")


def _funnel_counts(html: str) -> list:
    """The step counts, read off the RENDERED page rather than the
    query - "200% kept" was visible in the HTML and in nothing else."""
    return [int(m) for m in re.findall(
        r'<span class="funnel-n">(\d+)</span>', html)]


def _funnel_counts_narrow(html: str) -> bool:
    counts = _funnel_counts(html)
    return bool(counts) and counts == sorted(counts, reverse=True)


def phase_empty(tmp: Path) -> None:
    print("\n[phase 2] no trades, no costs, SPY cache absent")
    db_file = str(tmp / "empty.db")
    seed_empty_db(db_file)
    os.environ["CATALYST_BARS"] = str(tmp / "no-such-bars")

    with Served(db_file) as s:
        status, _, perf = s.get("/performance")
        check("GET /performance -> 200 on an empty database", status == 200)
        check("empty equity prints the raw query beside the zero",
              "SELECT position_id, account_mode, realized_pnl_cents" in perf)
        check("empty equity prints the row count",
              "rows returned: <b>0</b>" in perf)
        check("empty equity distinguishes 'no data' from 'broken query'",
              "this is an absence of data, not a fault" in perf)
        check("caveats are carried even with no data",
              "rode a lucky right-tail subsample" in perf
              and "Survivorship:" in perf)
        check("missing SPY cache is explained, not silently zero",
              "SPY benchmark unavailable" in perf and "No cached bars" in perf)

        status, _, funnel = s.get("/funnel")
        check("GET /funnel -> 200", status == 200)
        check("funnel names the stage responsible for no trades",
              "Why it has not traded" in funnel
              and "Risk engine approved a trade" in funnel,
              funnel[funnel.find("blame"):][:400])
        check("funnel names the largest drop reason in ENGLISH",
              "bigger than this account is allowed to lose" in funnel)
        check("the machine code is still there beside the English",
              "adverse_gap_assumption_exceeds_max_loss_per_position" in funnel)
        check("funnel shows the binding hard limit",
              "max_loss_per_position (hard)" in funnel)
        check("normal attrition is NOT painted as a fault",
              "Why they stopped here" in funnel
              and 'class="funnel-fault"' not in
                  funnel.split("Why they stopped here")[1].split("</div>")[0],
              "candidates stopping is the system working; only faults get "
              "the warning colour")
        check("no funnel step is wider than the one above it",
              _funnel_counts_narrow(funnel), _funnel_counts(funnel))
        check("funnel page ids stay unique", not duplicate_ids(funnel),
              str(duplicate_ids(funnel)))

        status, _, logs = s.get("/logs")
        check("GET /logs -> 200 on an empty log", status == 200)
        check("an empty log prints its own query and row count",
              "rows returned: <b>0</b>" in logs
              and "FROM logs ORDER BY" in logs
              and "absence of data, not a fault" in logs,
              logs[logs.find("Empty result"):][:300])

    # A database predating the logs table - what an in-place upgrade of an
    # old install actually looks like. init_db now always creates it, so
    # the only way to reach this branch is to take it away.
    stale = str(tmp / "no-logs-table.db")
    seed_empty_db(stale)
    conn = sqlite3.connect(stale)
    conn.execute("DROP TABLE logs")
    conn.commit()
    conn.close()
    with Served(stale) as s:
        status, _, logs = s.get("/logs")
        check("GET /logs -> 200 when the logs table is absent", status == 200)
        check("absent logs table is named, not blank",
              "logs table is not in this database" in logs
              and "schema_logs.sql" in logs,
              logs[logs.find("Empty result"):][:300])

        status, _, costs = s.get("/costs")
        check("zero spend prints its query and the raw upstream payload",
              "Empty result" in costs and "FROM cost_events" in costs)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        phase_traded(tmp)
        phase_empty(tmp)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name, detail in FAIL:
        print(f"  FAILED: {name}\n          {detail}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
