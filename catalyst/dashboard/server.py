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
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from catalyst.dashboard import panels, queries
from catalyst.dashboard.build import BUILD_HASH, build_manifest
from catalyst.dashboard.db import Db, db_path
from catalyst.dashboard.redact import redact_obj
from catalyst.dashboard.render import (
    alarm,
    digest,
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
    # Broker value FIRST. What the account is actually worth at Alpaca is
    # the one number the owner opens this page for; it used to sit below
    # a performance panel that leads with a comparison unavailable in the
    # account's first days.
    # THE OVERVIEW IS A SUMMARY. Each panel renders here with its
    # explanation folded into one disclosure (render.digest); the
    # dedicated page for that panel, one click away in the nav, still
    # shows every word inline. Rendering all of it here came to 76 words
    # of prose per figure, which is how the page came to read as an
    # essay with numbers in it rather than an instrument.
    body = (
        panels.state_line(db, p="state")
        + digest(panels.value_reconciliation_panel(db, p="ovval"))
        + digest(panels.performance_panel(db, p="perf"))
        + digest(panels.funnel_panel(db, p="funnel"))
        + digest(panels.cost_panel(db, p="ovcost", compact=True))
        + digest(panels.alerts_panel(db, p="alerts"))
    )
    return render_page("Overview", body, "/", db.path, db=db)


def route_performance(db: Db, params: dict) -> str:
    return render_page("Performance",
                       panels.performance_panel(db, p="perf")
                       + panels.value_reconciliation_panel(db, p="val"),
                       "/performance", db.path, db=db)


def route_trades(db: Db, params: dict) -> str:
    return render_page("Trades", panels.trades_panel(db, params, p="tr"),
                       "/trades", db.path, db=db)


def route_funnel(db: Db, params: dict) -> str:
    # The origin split sits UNDER the funnel because it answers the
    # question the funnel raises: candidates stopped here - whose
    # candidates? Its own tab would hide it from the person already
    # looking at exactly the right page.
    return render_page("Funnel",
                       panels.funnel_panel(db, p="funnel")
                       + panels.origin_panel(db, p="origin"),
                       "/funnel", db.path, db=db)


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
    # Simple by default: the full dossier is the record, not the read.
    # Anyone who needs every query is one click away and the link says so.
    wants_full = (params.get("view") or ["simple"])[0] == "full"
    if not cid:
        body = section("tr-section", "Decision trace",
                       "<p>Give a candidate_id: <code>/decision?candidate_id=...</code>. "
                       "The <a href='/decisions'>decisions list</a> links to each one.</p>")
    elif wants_full:
        body = panels.trace_page(db, cid, p="tr")
    else:
        body = panels.trace_simple(db, cid, p="trs")
    return render_page("Decision trace", body, "/decisions", db.path, db=db)


def route_chain(db: Db, params: dict) -> str:
    return render_page(
        "Every decision",
        panels.chain_panel(db) + panels.open_positions_panel(db),
        "/chain", db.path, db=db)


def route_node(db: Db, params: dict) -> str:
    node_id = (params.get("id") or [""])[0][:200]
    return render_page("Node", panels.node_panel(db, node_id, p="node"),
                       "/node", db.path, db=db,
                       subtitle="One node of the map, with its runbook")


#: Nodes drawn per layer before anyone asks for more. Deliberately
#: small: the map is for reading, and the wider views are one click.
DEFAULT_BRAIN_NODES = 8


def route_brain(db: Db, params: dict) -> str:
    def _num(key, default, lo, hi):
        """A pasted or edited URL must never 500 the page."""
        try:
            return max(lo, min(hi, float(params.get(key, [default])[0])))
        except (TypeError, ValueError, IndexError):
            return default

    zoom = _num("zoom", 1.0, 1.0, 3.0)
    # A SMALLER DEFAULT. Owner-reported: "its got too much data all at
    # once and isnt easy to navigate." Fourteen per layer across six
    # layers is eighty-odd nodes before anyone has asked a question;
    # eight fits without scrolling and the wider views are one click.
    nodes = int(_num("nodes", DEFAULT_BRAIN_NODES, 1, 999))
    focus = (params.get("focus") or [""])[0][:200]
    return render_page("The brain",
                       panels.brain_panel(db, p="brain", zoom=zoom,
                                          nodes=nodes, focus=focus),
                       "/brain", db.path, db=db,
                       subtitle="Every line is one recorded link")


def route_newsmap(db: Db, params: dict) -> str:
    return render_page("News map", panels.news_map_panel(db, params,
                                                         p="newsmap"),
                       "/newsmap", db.path, db=db,
                       subtitle="Every line is one stored story")


def route_integrity(db: Db, params: dict) -> str:
    """Where every number came from, and whether anything disagreed."""
    return render_page("Data integrity",
                       panels.data_integrity_panel(db, p="integ"),
                       "/integrity", db.path, db=db,
                       subtitle="Fill against intended, and the evidence "
                                "behind every price")


def route_story(db: Db, params: dict) -> str:
    """One news story and what the bot made of it.

    Reached by clicking a headline on the news map, which is what the
    owner asked for: "I want to be able to click the news and see what
    the bot thought of each and connectiosn".
    """
    return render_page("News story", panels.story_panel(db, params, p="story"),
                       "/newsmap", db.path, db=db,
                       subtitle="What was said, and what the bot made of it")


def route_refusals(db: Db, params: dict) -> str:
    # Simple by default, same as Decisions: the map answers "is a reason
    # refusing money" by following a strand; the table makes you compute
    # it in your head.
    if (params.get("view") or ["simple"])[0] == "full":
        body = panels.refusals_panel(db, p="ref")
    else:
        body = panels.refusals_simple(db, p="refs")
    # THE EVIDENCE AND WHAT IT MOVED, ON ONE PAGE. Refusals are the
    # input to the adaptation loop and the loop now actually runs, so
    # "what did declining these do to the thresholds" is answerable in
    # one place instead of two. It was only ever rendered inside the
    # Overview digest, which is the one page a reader skims.
    body += panels.section("adapt-section",
                           "What the evidence has moved so far",
                           panels.adaptation_block(db, p="adapt"))
    return render_page("Learning", body, "/refusals", db.path, db=db)


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

    # RUN THEM BY DEFAULT. Every active check is free - Alpaca and its
    # market data are in the subscription, EDGAR is public and keyless,
    # the Anthropic admin read costs no tokens - and the ordinary
    # Anthropic key is deliberately never probed because that one WOULD
    # cost money. So the click bought nothing and hid the answer behind
    # it (owner-reported 2026-08-11: "why do i need to click check
    # outside services now, why cant it just load"). ?check=skip is
    # there for anyone who wants the page without the round trips.
    run_active = params.get("check") != ["skip"]
    creds = None
    if run_active:
        try:
            from catalyst.setup.credentials import load_credentials
            creds = load_credentials()
        except Exception:  # noqa: BLE001 - unconfigured is a state, not an error
            creds = None
    report = maintenance.build_report(db, creds, run_active=run_active)
    # The result of a POST to /set-benchmark comes back as a query
    # parameter so the redirect can be followed and refreshed without
    # re-submitting the form. The MESSAGE itself is built here from a
    # code constant, never from the URL - a message rendered out of a
    # query string is a way to put words on the page from a link.
    message, failed = "", False
    if params.get("baseline") == ["ok"]:
        message = ("Benchmark updated. The figures below and every "
                   "performance number now compare against it, and the "
                   "previous baseline is kept in the history.")
    return render_page("Maintenance",
                       panels.maintenance_panel(report, p="maint")
                       + panels.benchmark_panel(db, p="bench",
                                                message=message, failed=failed)
                       + panels.schedule_panel(db, p="sched"),
                       "/maintenance", db.path, db=db)


def route_setup(db: Db, params: dict) -> str:
    """STAGE 7 MOUNT POINT. Replace the body of this function with the
    real credential form; the route, the no-store headers and the
    redaction layer are already in place around it."""
    return render_page("Setup", panels.setup_stub(p="setup"), "/setup", db.path, db=db)



#: WHAT TO COLLECT FOR WHICH QUESTION. The owner asked for "different
#: type of log collection buttons for different issues e.g. pricing or
#: logic etc. Then one master log that is all".
#:
#: A scoped bundle is not a smaller master bundle for its own sake: it
#: is the difference between sending a 4MB dump and sending the twelve
#: fields that answer the question. Each scope names the tables and the
#: log components that a person investigating THAT question actually
#: reads.
#:
#: `all` is the master and stays the default, so an existing link or
#: bookmark keeps returning everything it always did.
#: Rows per table in the full dump. High enough that a real database
#: fits whole, low enough that one runaway table cannot make the file
#: unopenable. Truncation is always DECLARED, never silent.
FULL_DUMP_ROWS_PER_TABLE = 20_000


class _Skipped(Exception):
    """This section is outside the requested scope."""


DIAGNOSTIC_SCOPES = {
    "all": {
        "label": "Overview",
        "why": "a readable summary - the funnel, the cost ledger, recent "
               "logs and errors, and a row count per table. Good for a "
               "quick look; NOT a complete record.",
        "tables": None,          # None = every table
        "sections": None,        # None = every section
        "log_components": None,  # None = every component
        "full_rows": False,
    },
    "everything": {
        "label": "EVERYTHING (full dump)",
        "why": "every row of every table, verbatim, with no summarising "
               "and no limits. Large. This is the one to send when you "
               "want the whole picture dissected rather than a report.",
        "tables": None,
        "sections": None,
        "log_components": None,
        "full_rows": True,
    },
    "pricing": {
        "label": "Cost & pricing",
        "why": "spend, the rate table, reconciliation against the bill, "
               "and every governor decision. Send this for a wrong "
               "figure, a surprise charge, or spending that stopped.",
        "tables": ("cost_events", "cost_governor_events",
                   "cost_reconciliation_events", "cost_reprice_events",
                   "pricing_overrides"),
        "sections": ("cost",),
        "log_components": ("catalyst.cost", "catalyst.research"),
    },
    "logic": {
        "label": "Decisions & logic",
        "why": "the funnel, candidates, the prompt the model saw, its "
               "reply verbatim, what it concluded and what the risk "
               "engine then did. Send this when it traded something odd, "
               "or refused something it should not have.",
        # research_call_turns is the MODEL'S SIDE OF THE CONVERSATION.
        # Without it this scope carries what the bot decided but not what
        # it was told, which is exactly the half you need to tell a wrong
        # thesis from an unlucky one. limit_applications is the same
        # story for the risk engine: which rule bound, and by how much.
        "tables": ("candidates", "research_calls", "research_call_turns",
                   "research_views", "risk_decisions", "limit_applications",
                   "refusals", "adaptive_param_log", "position_reviews"),
        "sections": ("funnel",),
        "log_components": ("catalyst.research", "catalyst.risk",
                           "catalyst.orchestrator"),
    },
    "data": {
        "label": "Feeds & data",
        "why": "what was read, what failed to read, and the evidence "
               "graph. Send this when a feed looks stuck or a source is "
               "missing.",
        "tables": ("raw_events", "raw_events_errors", "edgar_filings",
                   "graph_entities", "graph_assertions"),
        "sections": (),
        "log_components": ("catalyst.data", "catalyst.scheduler"),
    },
    "execution": {
        "label": "Orders & positions",
        "why": "orders, fills, positions, stops and closed trades. Send "
               "this for anything about what the broker actually did.",
        "tables": ("orders", "fills", "positions", "closed_trades",
                   "stop_replacements", "stop_confirmations",
                   "kill_switch_events", "equity_snapshots"),
        "sections": (),
        "log_components": ("catalyst.execution", "catalyst.orchestrator"),
    },
}


#: Offered in the UI as "how far back". None = everything.
LOG_WINDOW_DAYS = (1, 7, 30, 90, None)
DEFAULT_WINDOW_DAYS = 7

#: Candidate timestamp columns, most specific first. The window is
#: applied by NAMING the column a table actually has rather than by a
#: hardcoded table->column map, which would rot silently the first time
#: a table was added: a table whose time column nobody remembered to
#: register would be exported in full while the bundle claimed a window.
_TIME_COLUMNS = (
    "ts", "at", "called_at", "decided_at", "reconciled_at", "changed_at",
    "fetched_at", "attempted_at", "discovered_at", "refused_at",
    "reviewed_at", "priced_at", "closed_at", "opened_at", "placed_at",
    "submitted_at", "filled_at", "confirmed_at", "replaced_at", "set_at",
    "first_seen_at", "asserted_at", "recorded_at", "created_at", "run_at",
    "repriced_at", "taken_at", "triggered_at", "checked_at",
    "nominated_at",
)


def _time_column(db: Db, table: str) -> str:
    cols = set(db.columns(table))
    return next((c for c in _TIME_COLUMNS if c in cols), "")


def window_days(raw) -> int | None:
    """Coerce a ?days= value. A hostile or absent one falls back to the
    default rather than raising - a diagnostic export must not be the
    thing that 500s while someone is diagnosing something else."""
    try:
        n = int(str(raw))
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS
    if n <= 0:
        return None                       # 0 or negative = no window
    return min(n, 3650)


def diagnostics_bundle(db: Db, scope: str = "all", days: int | None = None) -> dict:
    """One-click diagnostic export, credentials redacted.

    Redaction runs over the WHOLE bundle after assembly as well as at
    each capture site, because a single missed field is the whole
    incident.
    """
    bundle = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "build_hash": BUILD_HASH,
        "build_manifest": build_manifest(),
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
    spec = DIAGNOSTIC_SCOPES.get(scope) or DIAGNOSTIC_SCOPES["all"]
    # SAY WHAT WAS COLLECTED, and what was deliberately left out. A
    # scoped bundle that does not announce its scope reads as a complete
    # one with things mysteriously missing.
    bundle["scope"] = scope if scope in DIAGNOSTIC_SCOPES else "all"
    bundle["scope_covers"] = spec["why"]
    bundle["scope_note"] = (
        "This is the MASTER bundle - nothing is filtered out."
        if spec["tables"] is None else
        "This bundle is SCOPED. Every row of the tables below is here "
        "verbatim; tables and log components outside the scope are "
        "omitted on purpose, not missing. Use "
        "/diagnostics.json?scope=everything for the whole database.")
    # EVERY ROW, VERBATIM, when asked for. The overview bundle carries
    # counts; a count cannot be dissected. The owner asked for "a log
    # that is literally every and anything so you can dissect it, i dont
    # want you to be missing any data" - so this reads whole tables,
    # applies the same redaction as everything else, and states plainly
    # if any table had to be truncated rather than silently shortening
    # it (a bundle that quietly drops rows is worse than one that says
    # it could not carry them).
    #
    # A SCOPED BUNDLE CARRIES ITS ROWS TOO. Verified by running before
    # this changed: scope=logic - labelled "what the model concluded and
    # what the risk engine did" - contained neither the prompt, the
    # model's reply, nor the thesis. Only counts. "research_views: 1"
    # answers no question anyone would send a bundle to ask.
    # The Overview keeps its counts; that is what it is for.
    # HOW FAR BACK. Owner-asked: "When i click download log i want it to
    # ask me how many days of logs so im not getting a massive file."
    # The window is stated, and so is every table it could NOT be
    # applied to - a table with no timestamp column comes out whole, and
    # saying so is the difference between a short file and a wrong one.
    cutoff = ""
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    bundle["window_days"] = days
    bundle["window_note"] = (
        f"Only rows timestamped on or after {cutoff} are included. "
        "Tables listed under window_not_applicable have no timestamp "
        "column and are included in full."
        if days else "No time window - everything, however old.")

    row_tables = spec["tables"]
    if spec.get("full_rows") or row_tables is not None:
        bundle["rows"] = {}
        bundle["rows_truncated"] = {}
        bundle["window_applied_to"] = {}
        bundle["window_not_applicable"] = []
        present = set(db.tables())
        if row_tables is not None:
            # A NAMED TABLE THAT IS NOT HERE IS SAID OUT LOUD. A scope
            # naming a table this database does not have used to produce
            # a silently smaller file - a renamed table would empty a
            # bundle and nothing would say so. (Five of them did: the
            # rate table, the adaptive log and the whole evidence graph
            # were all named wrongly and all silently absent.)
            absent = [t for t in row_tables if t not in present]
            if absent:
                bundle["scope_tables_absent"] = {
                    t: "named by this scope but not a table in this "
                       "database - it is missing, not empty" for t in absent}
        for name in sorted(present if row_tables is None
                           else present & set(row_tables)):
            col = _time_column(db, name) if cutoff else ""
            if cutoff:
                if col:
                    bundle["window_applied_to"][name] = col
                else:
                    bundle["window_not_applicable"].append(name)
            res = (db.q(f"SELECT * FROM {name} WHERE {col} >= ? LIMIT ?",
                        (cutoff, FULL_DUMP_ROWS_PER_TABLE + 1))
                   if col else
                   db.q(f"SELECT * FROM {name} LIMIT ?",
                        (FULL_DUMP_ROWS_PER_TABLE + 1,)))
            if res.error:
                bundle["rows"][name] = [{"query_error": res.error}]
                continue
            rows = res.dicts()
            if len(rows) > FULL_DUMP_ROWS_PER_TABLE:
                bundle["rows_truncated"][name] = (
                    f"more than {FULL_DUMP_ROWS_PER_TABLE} rows; the first "
                    f"{FULL_DUMP_ROWS_PER_TABLE} are included. Ask for this "
                    "table on its own with ?scope=everything&table=" + name)
                rows = rows[:FULL_DUMP_ROWS_PER_TABLE]
            bundle["rows"][name] = rows

    wanted = spec["tables"]
    for name in sorted(db.tables()):
        if wanted is not None and name not in wanted:
            continue
        res = db.count(name)
        bundle["row_counts"][name] = res.scalar(0) if not res.error else res.error

    if spec["sections"] is not None and "funnel" not in spec["sections"]:
        bundle.pop("funnel", None)
    try:
        if "funnel" not in bundle:
            raise _Skipped()
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
    except _Skipped:
        pass
    except Exception:
        bundle["funnel"] = {"error": traceback.format_exc()}

    if spec["sections"] is not None and "cost" not in spec["sections"]:
        bundle.pop("cost", None)
    try:
        if "cost" not in bundle:
            raise _Skipped()
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
    except _Skipped:
        pass
    except Exception:
        bundle["cost"] = {"error": traceback.format_exc()}

    errs = db.q("SELECT source, attempted_at, error_text FROM raw_events_errors "
                + ("WHERE attempted_at >= ? " if cutoff else "")
                + "ORDER BY attempted_at DESC LIMIT ?",
                ((cutoff,) if cutoff else ())
                + (FULL_DUMP_ROWS_PER_TABLE if spec.get("full_rows") else 50,))
    bundle["recent_errors"] = errs.dicts() if not errs.error else [{"query_error": errs.error}]

    if db.table_exists("logs"):
        # A SCOPED BUNDLE CARRIES MORE OF ITS OWN LOGS, not fewer. The
        # point of narrowing is to spend the budget on lines that bear on
        # the question, so the cap rises when the components are filtered.
        components = spec["log_components"]
        if components:
            like = " OR ".join(["component LIKE ?"] * len(components))
            lg = db.q(
                "SELECT ts, level, component, message, cycle_id, "
                f"traceback_text, context_json FROM logs WHERE ({like}) "
                + ("AND ts >= ? " if cutoff else "")
                + "ORDER BY ts DESC LIMIT 1000",
                tuple(f"{c}%" for c in components)
                + ((cutoff,) if cutoff else ()))
            # An empty result must not read as "nothing went wrong": say
            # which components were asked for.
            if not lg.error and not lg.rows:
                bundle["recent_logs_note"] = (
                    "no log lines matched components "
                    + ", ".join(components)
                    + " - that is an empty result, not an absence of faults")
        elif spec.get("full_rows"):
            lg = db.q("SELECT ts, level, component, message, cycle_id, "
                      "traceback_text, context_json FROM logs "
                      + ("WHERE ts >= ? " if cutoff else "")
                      + "ORDER BY ts DESC LIMIT ?",
                      ((cutoff, FULL_DUMP_ROWS_PER_TABLE) if cutoff
                       else (FULL_DUMP_ROWS_PER_TABLE,)))
        else:
            lg = db.q("SELECT ts, level, component, message, cycle_id, "
                      "traceback_text, context_json FROM logs "
                      + ("WHERE ts >= ? " if cutoff else "")
                      + "ORDER BY ts DESC LIMIT 300",
                      (cutoff,) if cutoff else ())
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
        # Which copy of the code answered. install.sh polls this, and it
        # is the one place reachable with curl that can tell "the repo is
        # current" from "the running service is current" - they were not
        # the same thing on the owner's machine (2026-08-11).
        "source_dir": build_manifest()["directory"],
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
    "/trades": route_trades,
    "/funnel": route_funnel,
    "/costs": route_costs,
    "/decisions": route_decisions,
    "/decision": route_decision,
    "/brain": route_brain,
    "/node": route_node,
    "/chain": route_chain,
    "/newsmap": route_newsmap,
    # Not a tab of its own: it is always arrived at FROM the news map, so
    # it stays highlighted as that tab rather than adding a navigation
    # entry nobody could click without a story in mind.
    "/story": route_story,
    "/integrity": route_integrity,
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


def set_benchmark(db_file: str, form: dict) -> tuple[bool, str]:
    """Record an owner-set benchmark baseline: an amount and a date.

    Owner-asked: "I can say track SPY if i were to invest $2000 on a set
    date and calculate that against our bot."

    EVERY REFUSAL IS A SENTENCE. A hostile or empty value must never
    reach a traceback, and - the failure that actually costs something -
    must never be quietly accepted as zero: a baseline of $0 makes every
    percentage on the dashboard a division by nothing.

    This writes a comparison, not a limit. It cannot change what the bot
    may spend, how it sizes, or what it trades.
    """
    from datetime import date as _date

    from catalyst import benchmark
    from catalyst.dashboard.panels import (
        EARLIEST_BASELINE_DATE, MAX_BASELINE_CENTS, MIN_BASELINE_CENTS,
    )
    from catalyst.storage import init_db

    raw_amount = str(form.get("amount_usd") or "").strip()
    if not raw_amount:
        return False, ("Give the amount you would have put into SPY, in "
                       "dollars - for example 2000. Nothing was changed.")
    cleaned = raw_amount.replace("$", "").replace(",", "").replace(" ", "")
    try:
        dollars_in = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return False, (f"{raw_amount!r} is not an amount of money. Enter "
                       "digits, for example 2000 or 2000.50. Nothing was "
                       "changed.")
    # Decimal accepts "NaN" and "Infinity" happily, and both would sail
    # through a > 0 test and poison every figure downstream.
    if not dollars_in.is_finite():
        return False, (f"{raw_amount!r} is not a finite amount of money. "
                       "Nothing was changed.")
    cents = (dollars_in * 100).quantize(Decimal("1"))
    if cents <= 0:
        return False, ("The comparison amount must be more than zero - "
                       "'what if I had put nothing in SPY' has no answer, "
                       "and a zero baseline would make every percentage on "
                       "the dashboard a division by nothing. Nothing was "
                       "changed.")
    if cents < MIN_BASELINE_CENTS:
        return False, (f"${dollars_in} is below the $1 minimum this form "
                       "accepts. Nothing was changed.")
    if cents > MAX_BASELINE_CENTS:
        return False, (f"${dollars_in:,} is above the $10,000,000 maximum "
                       "this form accepts - that is a guard against a "
                       "mistyped figure, not a view about your account. "
                       "Nothing was changed.")

    raw_date = str(form.get("start_date") or "").strip()
    if not raw_date:
        return False, ("Give the date you would have bought SPY, as "
                       "YYYY-MM-DD. Nothing was changed.")
    try:
        start = _date.fromisoformat(raw_date)
    except ValueError:
        return False, (f"{raw_date!r} is not a date this form can read. Use "
                       "YYYY-MM-DD, for example 2026-07-01. Nothing was "
                       "changed.")
    today = datetime.now(timezone.utc).date()
    if start > today:
        return False, (f"{start} is in the future. A benchmark has to start "
                       "on a day the market has already traded, so the "
                       f"latest date this accepts is {today}. Nothing was "
                       "changed.")
    earliest = _date.fromisoformat(EARLIEST_BASELINE_DATE)
    if start < earliest:
        return False, (f"{start} is before {EARLIEST_BASELINE_DATE}. SPY did "
                       "not exist to buy then, and no bar cache can answer "
                       "it. Nothing was changed.")

    why = " ".join(str(form.get("reason") or "").split())[:500]
    reason = (f"set by hand on the Maintenance page: track SPY as if "
              f"${cents / 100:,.2f} had been invested on {start}. Closed "
              "trades and API spend before that date are outside the "
              "comparison.")
    if why:
        reason += f" Owner's note: {why}"

    try:
        # init_db is CREATE TABLE IF NOT EXISTS throughout, so this is a
        # safe migration for a database made before benchmark_baselines
        # existed. Without it the owner's only route to a working page
        # would be an upgrade they cannot run from the browser.
        conn = init_db(db_file)
    except Exception as exc:  # noqa: BLE001 - the owner reads this
        return False, (f"the database at {db_file} could not be opened for "
                       f"writing: {type(exc).__name__}: {exc}")
    try:
        benchmark.record(conn, capital_cents=cents, start_date=start,
                         source="owner_set", account_fingerprint="",
                         reason=reason)
    except Exception as exc:  # noqa: BLE001
        return False, (f"the baseline could not be written: "
                       f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()
    return True, (f"Benchmark set: SPY bought with ${cents / 100:,.2f} on "
                  f"{start}. Every performance figure now compares against "
                  "that, and the previous baseline is kept in the history "
                  "below.")


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

    def _send(self, code: int, body: bytes, content_type: str,
              extra: dict | None = None):
        self.send_response(code)
        _no_store_headers(self, content_type, len(body))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_html(self, code: int, html_doc: str):
        self._send(code, html_doc.encode("utf-8"), "text/html; charset=utf-8")

    def _send_json(self, code: int, payload: dict, filename: str = ""):
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        extra = ({"Content-Disposition": f'attachment; filename="{filename}"'}
                 if filename else None)
        self._send(code, body, "application/json; charset=utf-8", extra=extra)

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
                # AS A DOWNLOAD, not a tab full of JSON. Without a
                # Content-Disposition the browser renders it inline, so
                # the "export a diagnostic bundle" button opened a wall
                # of text the owner then had to select and copy by hand
                # (owner-reported). The brief asks for ONE CLICK.
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                params = parse_qs(parsed.query or "")
                raw = (params.get("scope") or ["all"])[0]
                scope = raw if raw in DIAGNOSTIC_SCOPES else "all"
                days = window_days((params.get("days") or [None])[0])
                span = f"{days}d" if days else "all"
                return self._send_json(
                    200, diagnostics_bundle(db, scope=scope, days=days),
                    filename=f"catalyst-{scope}-{span}-{stamp}.json")
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
        # READ THE BODY EXACTLY ONCE. The /setup branch below used to read
        # it and then fall through to a second read of the same
        # Content-Length; when the setup app declined the request (nothing
        # mounted, or a sub-path it does not handle) that second read
        # blocked on a socket with nothing left to send, and the request
        # thread hung until the client timed out. Found by running
        # scripts/dashboard_smoke.py, which hung on exactly that call.
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        if self.path.startswith("/setup"):
            if self._delegate_setup("POST", body):
                return
        parsed = urlparse(self.path)
        raw_body = body.decode("utf-8", "replace")
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

        if parsed.path == "/set-benchmark":
            okay, message = set_benchmark(db_file, form)
            if okay:
                self.send_response(303)
                self.send_header("Location", "/maintenance?baseline=ok&check=skip")
                _no_store_headers(self, "text/plain", 0)
                self.end_headers()
                return
            db = Db(db_file)
            try:
                body = render_page(
                    "Benchmark not changed",
                    section("bench-fail", "Benchmark not changed",
                            alarm(esc(message))
                            + "<p>Nothing was written. The baseline in force "
                            "is unchanged.</p>"
                            "<p><a href='/maintenance'>back to the "
                            "maintenance page</a></p>"),
                    "/maintenance", db.path, db=db)
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
