"""The dashboard server: stdlib http.server, server-rendered HTML.

No framework, no npm, no build step - the dependency list stays at
"python3". Bound to 0.0.0.0:8000 per the brief; the VPS is IP
restricted.

Read-only over the database EXCEPT two endpoints, both explicit:
  POST /acknowledge-reconciliation  -> cost.tracker.acknowledge_discrepancy
  POST /set-token-price             -> cost.overrides.set_override
  POST /setup                       -> STUB, stage 7 owns the real flow

Every response carries Cache-Control: no-store and a build hash, so a
stale browser and a failed deploy stop looking identical.
"""

import json
import re
import sys
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from catalyst.dashboard import panels, queries
from catalyst.dashboard.build import BUILD_HASH
from catalyst.dashboard.db import Db, db_path
from catalyst.dashboard.redact import redact_obj
from catalyst.dashboard.render import (
    alarm,
    dollars,
    duplicate_ids,
    esc,
    page,
    pre,
    section,
    signed_pp,
)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000


def _no_store_headers(handler: BaseHTTPRequestHandler, content_type: str, length: int):
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(length))
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Expires", "0")
    handler.send_header("X-Catalyst-Build", BUILD_HASH)


def _rail_for(db: Db) -> str:
    """The always-visible state strip. Built ONLY from figures the pages
    already compute - it reads, it never decides."""
    from catalyst.dashboard.render import status_rail

    items = []
    try:
        perf = queries.performance(db)
        equity = perf.net_equity_cents
        items.append(("Account", dollars(equity),
                      "good" if perf.bot_points else "idle"))
        excess = perf.excess_pp
        items.append((
            "vs S&P",
            esc(signed_pp(excess)) if excess is not None else "&mdash;",
            "idle" if excess is None else ("good" if excess >= 0 else "crit")))
        items.append(("Closed trades", str(perf.n_closed),
                      "good" if perf.n_closed else "idle"))
    except Exception:  # noqa: BLE001 - the rail must never break a page
        items.append(("Account", "unavailable", "warn"))
    try:
        c = queries.cost_panel(db)
        used = (float(c.scheduled_mtd_cents) / float(c.base_cap_cents) * 100
                if c.base_cap_cents else 0.0)
        items.append((
            "Spend this month",
            f"{dollars(c.scheduled_mtd_cents)} of {dollars(c.base_cap_cents)}",
            "crit" if used >= 100 else ("warn" if used >= 75 else "good")))
    except Exception:  # noqa: BLE001
        items.append(("Spend this month", "unavailable", "warn"))
    try:
        open_positions = db.q(
            "SELECT COUNT(*) n FROM positions WHERE status = 'open'")
        n = open_positions.rows[0]["n"] if open_positions.rows else 0
        items.append(("Open positions", f"{n} of 5",
                      "good" if n else "idle"))
    except Exception:  # noqa: BLE001
        items.append(("Open positions", "unavailable", "warn"))
    return status_rail(items)


def render_page(title: str, body: str, active: str, path: str,
                db: Db | None = None, subtitle: str = "") -> str:
    """Build the page, then CHECK IT. A duplicated element id becomes a
    banner on the page rather than two silently blank panels."""
    rail = _rail_for(db) if db is not None else ""
    html_doc = page(title, body, active, path, rail=rail, subtitle=subtitle)
    dupes = duplicate_ids(html_doc)
    if dupes:
        banner = alarm(
            "<b>Duplicate element ids on this page: </b>"
            + esc(", ".join(dupes))
            + ". One panel may be receiving data meant for another. This banner is "
            "a bug report against the dashboard itself, not against the bot."
        )
        # Match the OPENING <main ...> tag rather than the literal
        # "<main>": the shell grew an id attribute and this injection
        # silently stopped firing, which would have hidden the very
        # banner that exists to stop panels failing silently.
        html_doc, n = re.subn(r"(<main\b[^>]*>)", r"\1" + banner.replace("\\", "\\\\"),
                              html_doc, count=1)
        if not n:      # the shell changed shape again - never lose the warning
            html_doc = html_doc.replace("</body>", banner + "</body>", 1)
    return html_doc


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def route_overview(db: Db, params: dict) -> str:
    body = (
        panels.performance_panel(db, p="perf")
        + panels.value_reconciliation_panel(db, p="ovval")
        + panels.funnel_panel(db, p="funnel")
        + panels.cost_panel(db, p="ovcost", compact=True)
        + panels.alerts_panel(db, p="alerts")
    )
    return render_page("Overview", body, "/", db.path, db=db)


def route_performance(db: Db, params: dict) -> str:
    return render_page("Performance",
                       panels.performance_panel(db, p="perf")
                       + panels.value_reconciliation_panel(db, p="val"),
                       "/performance", db.path, db=db)


def route_funnel(db: Db, params: dict) -> str:
    return render_page("Funnel", panels.funnel_panel(db, p="funnel"), "/funnel",
                       db.path, db=db)


def route_costs(db: Db, params: dict) -> str:
    banner = ""
    if params.get("ack") == ["ok"]:
        banner = section(
            "cost-ack-result", "Acknowledged",
            "<p>The reconciliation discrepancy was acknowledged and recorded with "
            "your name and the time. Scheduled spend resumes on the next "
            "authorization check.</p>",
        )
    return render_page("Cost", banner + panels.cost_panel(db, p="cost"), "/costs",
                       db.path, db=db)


def route_decisions(db: Db, params: dict) -> str:
    return render_page("Decisions", panels.decisions_index(db, p="dec"),
                       "/decisions", db.path, db=db)


def route_decision(db: Db, params: dict) -> str:
    cid = (params.get("candidate_id") or [""])[0]
    if not cid:
        body = section("tr-section", "Decision trace",
                       "<p>Give a candidate_id: <code>/decision?candidate_id=...</code>. "
                       "The <a href='/decisions'>decisions list</a> links to each one.</p>")
    else:
        body = panels.trace_page(db, cid, p="tr")
    return render_page("Decision trace", body, "/decisions", db.path, db=db)


def route_refusals(db: Db, params: dict) -> str:
    return render_page("Refusals", panels.refusals_panel(db, p="ref"),
                       "/refusals", db.path, db=db)


def route_logs(db: Db, params: dict) -> str:
    flat = {k: v[0] for k, v in params.items() if v}
    return render_page("Logs", panels.logs_panel(db, flat, p="log"), "/logs",
                       db.path, db=db)


def route_maintenance(db: Db, params: dict) -> str:
    """Passive by default; contacts outside services only when asked.

    Every active probe is free (Alpaca and EDGAR cost nothing, the
    Anthropic probe reads the bill rather than the model), but they are
    still opt-in: a page that fires four network requests on every
    refresh is a page that hammers a rate-limited public API.
    """
    from catalyst.dashboard import maintenance

    run_active = params.get("check") == ["now"]
    creds = None
    if run_active:
        try:
            from catalyst.setup.credentials import load_credentials
            creds = load_credentials()
        except Exception:  # noqa: BLE001 - unconfigured is a state, not an error
            creds = None
    report = maintenance.build_report(db, creds, run_active=run_active)
    return render_page("Maintenance", panels.maintenance_panel(report, p="maint"),
                       "/maintenance", db.path, db=db)


def route_setup(db: Db, params: dict) -> str:
    """STAGE 7 MOUNT POINT. Replace the body of this function with the
    real credential form; the route, the no-store headers and the
    redaction layer are already in place around it."""
    return render_page("Setup", panels.setup_stub(p="setup"), "/setup", db.path, db=db)


def diagnostics_bundle(db: Db) -> dict:
    """One-click diagnostic export, credentials redacted.

    Redaction runs over the WHOLE bundle after assembly as well as at
    each capture site, because a single missed field is the whole
    incident.
    """
    bundle = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "build_hash": BUILD_HASH,
        "python": sys.version,
        "db_path": db.path,
        "db_open_error": db.open_error,
        "tables_present": sorted(db.tables()),
        "row_counts": {},
        "funnel": {},
        "cost": {},
        "recent_errors": [],
        "recent_logs": [],
        "note": (
            "Credentials are redacted at capture and again on the way out. "
            "Env-var-shaped assignments, sk-ant-* and PK/AK-* strings, and "
            "secret-named JSON values are masked. Environment VALUES are never "
            "included at all - only the names below."
        ),
        "env_var_names_only": sorted(__import__("os").environ.keys()),
    }
    for name in sorted(db.tables()):
        res = db.count(name)
        bundle["row_counts"][name] = res.scalar(0) if not res.error else res.error

    try:
        f = queries.funnel(db)
        bundle["funnel"] = {
            "blame": f.blame,
            "blame_stage": f.blame_stage,
            # NB the field is "stage", not "key": redact_obj masks the VALUE
            # of any dict key whose NAME contains "key" (deliberate
            # over-redaction), which would otherwise blank this out.
            "stages": [
                {"stage": s.key, "count": s.count,
                 "drops": [{"reason": r, "n": n} for r, n, _ in s.drops]}
                for s in f.stages
            ],
        }
    except Exception:
        bundle["funnel"] = {"error": traceback.format_exc()}

    try:
        c = queries.cost_panel(db)
        bundle["cost"] = {
            "month": c.month_prefix,
            "scheduled_mtd_cents_local_estimate": str(c.scheduled_mtd_cents),
            "manual_mtd_cents_local_estimate": str(c.manual_mtd_cents),
            "lifetime_manual_cents": str(c.lifetime_manual_cents),
            "billed_total_cents_closed_days": str(c.billed_total_cents),
            "unacknowledged_discrepancies": c.unacked_q.row_count,
            "unpriced_rows": c.unpriced_q.row_count,
            "rates_stale": c.rates_stale,
            "ledger_crosscheck": c.ledger_crosscheck,
        }
    except Exception:
        bundle["cost"] = {"error": traceback.format_exc()}

    errs = db.q("SELECT source, attempted_at, error_text FROM raw_events_errors "
                "ORDER BY attempted_at DESC LIMIT 50")
    bundle["recent_errors"] = errs.dicts() if not errs.error else [{"query_error": errs.error}]

    if db.table_exists("logs"):
        lg = db.q("SELECT ts, level, component, message, cycle_id, traceback_text, "
                  "context_json FROM logs ORDER BY ts DESC LIMIT 300")
        bundle["recent_logs"] = lg.dicts() if not lg.error else [{"query_error": lg.error}]
    else:
        bundle["recent_logs"] = [{"note": "logs table absent; see "
                                          "catalyst/dashboard/schema_logs.sql"}]

    # The maintenance checks, so whoever receives this file sees the
    # same summary the owner saw. Passive only: producing a diagnostic
    # bundle must never make network calls of its own.
    try:
        from catalyst.dashboard import maintenance
        bundle["maintenance_checks"] = [
            {"name": c.name, "group": c.group, "state": c.state,
             "summary": c.summary, "raw": c.raw}
            for c in maintenance.passive_checks(db)
        ]
    except Exception as exc:  # noqa: BLE001 - a bundle must always render
        bundle["maintenance_checks"] = [{"error": repr(exc)}]

    return redact_obj(bundle)


def health(db: Db) -> dict:
    return {
        "build_hash": BUILD_HASH,
        "db_path": db.path,
        "db_open_error": db.open_error,
        "tables": sorted(db.tables()),
        "logs_table_present": db.table_exists("logs"),
        "graph_assertions_present": db.table_exists("graph_assertions"),
        "served_at": datetime.now(timezone.utc).isoformat(),
        "cache_policy": "no-store",
    }


HTML_ROUTES = {
    "/": route_overview,
    "/performance": route_performance,
    "/funnel": route_funnel,
    "/costs": route_costs,
    "/decisions": route_decisions,
    "/decision": route_decision,
    "/refusals": route_refusals,
    "/logs": route_logs,
    "/maintenance": route_maintenance,
    "/setup": route_setup,
}


# --------------------------------------------------------------------------
# Write endpoints
# --------------------------------------------------------------------------


def set_token_price(db_file: str, form: dict) -> tuple[bool, str]:
    """Record an owner-entered token rate, effective from a date.

    Validation lives in cost.overrides; this only shapes the form into
    its arguments and turns a refusal into a message. A bad rate must
    reach the owner as a sentence, never as a traceback or - far worse -
    as a silently accepted zero.
    """
    import sqlite3
    from datetime import date as _date

    from catalyst.cost.overrides import set_override

    model = (form.get("model") or "").strip()
    who = (form.get("set_by") or "").strip()
    try:
        effective = _date.fromisoformat((form.get("effective_from") or "").strip())
    except ValueError:
        return False, ("Give the date the new rate starts, as YYYY-MM-DD. "
                       "Rates apply from that day forward; earlier spending "
                       "keeps the rate it was priced at.")
    conn = sqlite3.connect(db_file)
    try:
        set_override(conn, model, effective,
                     form.get("input_cents_per_mtok"),
                     form.get("output_cents_per_mtok"),
                     set_by=who, note=(form.get("note") or "").strip(),
                     allow_large_change=bool(form.get("allow_large_change")))
    except Exception as exc:  # noqa: BLE001 - the owner reads this
        return False, str(exc)
    finally:
        conn.close()
    return True, "recorded"


def acknowledge(db_file: str, event_id: str, acknowledged_by: str) -> tuple[bool, str]:
    """The one write the dashboard owns. Opens its OWN read-write
    connection - the page-rendering handle is mode=ro and physically
    cannot do this."""
    from catalyst.cost.tracker import acknowledge_discrepancy
    from catalyst.storage import connect

    if not event_id:
        return False, "no event_id supplied"
    if not acknowledged_by.strip():
        return False, ("acknowledged_by is required: a discrepancy must be "
                       "acknowledged by a named human, never anonymously")
    conn = connect(db_file)
    try:
        acknowledge_discrepancy(conn, event_id, acknowledged_by.strip())
        return True, "acknowledged"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"catalyst-dashboard/{BUILD_HASH}"
    db_file: str = ""

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        _no_store_headers(self, content_type, len(body))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_html(self, code: int, html_doc: str):
        self._send(code, html_doc.encode("utf-8"), "text/html; charset=utf-8")

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def log_message(self, fmt, *args):  # quieter, and to stderr
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _delegate_setup(self, method: str, body: bytes = b"") -> bool:
        """Route /setup* to the mounted SetupApp (stage 7). Returns True
        when the request was handled."""
        app = getattr(self, "setup_app", None)
        if app is None or not self.path.startswith("/setup"):
            return False
        headers = {k.lower(): v for k, v in self.headers.items()}
        resp = app.handle(method, self.path, body, headers)
        payload = resp.body
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for name, value in resp.headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if self._delegate_setup("GET"):
            return
        app = getattr(self, "setup_app", None)
        if (app is not None and parsed.path == "/"
                and not app._is_configured()):
            # first run: the owner's link must land on the credential
            # form, not an empty dashboard (stress stage-8 E2)
            self.send_response(302)
            query = f"?{parsed.query}" if parsed.query else ""
            self.send_header("Location", f"/setup{query}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        db = Db(self.db_file or db_path())
        try:
            if parsed.path == "/health":
                return self._send_json(200, health(db))
            if parsed.path == "/diagnostics.json":
                return self._send_json(200, diagnostics_bundle(db))
            handler = HTML_ROUTES.get(parsed.path)
            if handler is None:
                return self._send_html(404, render_page(
                    "Not found",
                    section("nf-section", "404",
                            f"<p>No route <code>{esc(parsed.path)}</code>.</p>"),
                    "/", db.path))
            return self._send_html(200, handler(db, params))
        except Exception:
            tb = traceback.format_exc()
            body = render_page(
                "Error",
                section("err-section", "The dashboard itself failed",
                        "<p>This is a dashboard bug, not a bot state. The full "
                        "traceback is below and in the diagnostic bundle.</p>"
                        + pre(tb)),
                "/", db.path)
            return self._send_html(500, body)
        finally:
            db.close()

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if self.path.startswith("/setup"):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if self._delegate_setup("POST", body):
                return
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length).decode("utf-8") if length else ""
        form = {k: v[0] for k, v in parse_qs(raw_body).items()}
        db_file = self.db_file or db_path()

        if parsed.path == "/set-token-price":
            okay, message = set_token_price(db_file, form)
            if okay:
                self.send_response(303)
                self.send_header("Location", "/costs?price=ok")
                _no_store_headers(self, "text/plain", 0)
                self.end_headers()
                return
            db = Db(db_file)
            try:
                body = render_page(
                    "Rate change refused",
                    section("price-fail", "Rate change refused",
                            f"<p>{esc(message)}</p>"
                            "<p><a href=\"/costs\">Back to the cost page</a></p>"),
                    "/costs", db.path, db=db)
            finally:
                db.close()
            return self._send_html(400, body)

        if parsed.path == "/acknowledge-reconciliation":
            okay, message = acknowledge(
                db_file, form.get("event_id", ""), form.get("acknowledged_by", ""))
            if okay:
                self.send_response(303)
                self.send_header("Location", "/costs?ack=ok")
                _no_store_headers(self, "text/plain", 0)
                self.end_headers()
                return
            db = Db(db_file)
            try:
                body = render_page(
                    "Acknowledge failed",
                    section("ack-fail", "Acknowledgement refused",
                            alarm(esc(message))
                            + "<p><a href='/costs'>back to the cost page</a></p>"),
                    "/costs", db.path)
            finally:
                db.close()
            return self._send_html(400, body)

        if parsed.path.startswith("/setup"):
            if getattr(self, "setup_app", None) is None:
                # mount point exists but nothing is mounted (running the
                # dashboard standalone): say so honestly
                return self._send_json(501, {
                    "error": "not implemented",
                    "detail": "No setup app is mounted on this server. The "
                              "installed service mounts stage 7's SetupApp "
                              "here (catalyst.orchestrator.scheduler).",
                    "mount_point": panels.SETUP_MOUNT_POINT,
                    "build_hash": BUILD_HASH,
                })
            # handled in do_POST via _delegate_setup before we get here
        return self._send_json(404, {"error": f"no POST route {parsed.path}"})


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                db_file: str | None = None,
                setup_app=None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,),
                   {"db_file": db_file or db_path(),
                    "setup_app": setup_app})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="catalyst dashboard")
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help="bind address (default 0.0.0.0, per the brief)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", default=db_path(), help="sqlite path, or set CATALYST_DB")
    ap.add_argument("--diagnostics", action="store_true",
                    help="print the diagnostic bundle (credentials redacted) "
                         "to stdout and exit, without starting a server")
    args = ap.parse_args(argv)

    if args.diagnostics:
        # The bundle has to be obtainable when the PAGE is the thing that
        # is broken - which is exactly when it is most needed, and was
        # not possible while /diagnostics.json was the only route to it.
        db = Db(args.db)
        try:
            sys.stdout.write(json.dumps(diagnostics_bundle(db), indent=2,
                                        default=str) + "\n")
        finally:
            db.close()
        return 0

    httpd = make_server(args.host, args.port, args.db)
    sys.stderr.write(
        f"catalyst dashboard build {BUILD_HASH} on http://{args.host}:{args.port} "
        f"db={args.db}\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
