"""A readable front page for a downloaded diagnostic bundle.

OWNER-ASKED 2026-08-21: "when we download logs can we attach a html
reader for all the files ttached to make it easier for me to
troubleshoot before sending it to you, still include raw logs in the
folder".

Until now the export was a single JSON file. It is complete and it is
verbatim, which is what makes it worth sending - and it is also
unreadable by a person, so the only thing to do with it was send it on
and wait. This turns the same download into a folder with a front page,
without taking anything away: the raw JSON and a plain-text log are
still in there, byte for byte, to grep or forward.

THE ONE CONSTRAINT THAT DECIDES THE WHOLE DESIGN. A browser will not
let a page opened from a folder read a file beside it - `file://` fetch
is blocked, and it fails silently enough that the page just looks
broken. So the reader cannot load bundle.json at all; the data is
EMBEDDED in the page. That is why index.html is the large file and why
it works on a double-click with no server, no network and no install.

NOTHING IS SUMMARISED AWAY. The reader filters and formats what is
there; every row it can show comes from the same object that is sitting
in the folder next to it. If the page and the JSON ever disagreed, the
page would be worse than useless, so it never computes anything the
JSON does not already say.

REDACTION HAPPENS BEFORE THIS. The bundle handed here has already been
through redact_obj twice (at capture and on assembly). This module adds
no field and reads no credential; it only renders what it is given.
"""

from __future__ import annotations

import json

#: Kept in step with the dashboard's own dark palette so the two do not
#: look like different products. Deliberately a small, self-contained
#: copy: this page must render with no stylesheet, no font and no script
#: fetched from anywhere.
_CSS = """
:root{--page:#050608;--surface:#0c0e12;--surface2:#131720;--ink:#e8ecf3;
--ink2:#9aa6b8;--muted:#6b7688;--line:#1e2530;--accent:#4c9aff;
--pos:#2ecc71;--neg:#ff5d5d;--warn:#ffb020;--crit:#ff5d5d;}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
font:13px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:16px 22px;border-bottom:1px solid var(--line);
background:var(--surface)}
h1{margin:0 0 4px;font-size:15px;letter-spacing:.06em;text-transform:uppercase}
.sub{color:var(--ink2);font-size:12px}
main{padding:18px 22px;max-width:1500px}
section{background:var(--surface);border:1px solid var(--line);
margin:0 0 12px}
h2{margin:0;padding:7px 12px;font-size:10px;letter-spacing:.18em;
text-transform:uppercase;color:var(--accent);background:#0f141c;
border-bottom:1px solid var(--line);border-left:2px solid var(--accent)}
.body{padding:10px 12px}
.tiles{display:grid;gap:1px;background:var(--line);
grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.tile{background:var(--surface);padding:9px 12px}
.tile .k{font-size:9px;letter-spacing:.12em;text-transform:uppercase;
color:var(--muted);margin:0 0 3px}
.tile .v{font-size:20px;font-weight:600;margin:0;
font-variant-numeric:tabular-nums}
.tile .s{font-size:11px;color:var(--ink2);margin:2px 0 0}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{text-align:left;padding:3px 8px;border-bottom:1px solid var(--line);
vertical-align:top}
th{font-size:9px;letter-spacing:.1em;text-transform:uppercase;
color:var(--muted);position:sticky;top:0;background:#0f141c}
tr:hover td{background:#11161f}
.scroll{overflow:auto;max-height:70vh}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
input,select{font:inherit;background:var(--surface2);color:var(--ink);
border:1px solid var(--line);border-radius:3px;padding:4px 7px;max-width:100%}
label{display:inline-flex;align-items:center;gap:5px;margin-right:12px;
color:var(--ink2);font-size:12px}
.bar{display:flex;flex-wrap:wrap;gap:6px 0;align-items:center;
padding:8px 12px;border-bottom:1px solid var(--line);background:#0a0e14}
.pill{display:inline-block;padding:1px 6px;border-radius:2px;font-size:10px;
font-weight:700;letter-spacing:.04em}
.ERROR,.CRITICAL{background:rgba(255,93,93,.15);color:var(--neg)}
.WARNING{background:rgba(255,176,32,.15);color:var(--warn)}
.INFO{background:rgba(76,154,255,.13);color:var(--accent)}
.DEBUG{background:#161b24;color:var(--muted)}
pre{margin:4px 0 0;padding:7px 9px;background:#0a0e14;
border:1px solid var(--line);white-space:pre-wrap;word-break:break-word;
font-size:11px;color:var(--ink2);max-height:320px;overflow:auto}
details>summary{cursor:pointer;color:var(--accent);font-size:11px;
padding:2px 0}
.note{color:var(--ink2);font-size:12px;margin:0 0 8px}
.warnbox{border-left:3px solid var(--warn);background:rgba(255,176,32,.07);
padding:8px 11px;margin:0 0 10px;font-size:12px}
.critbox{border-left:3px solid var(--crit);background:rgba(255,93,93,.07);
padding:8px 11px;margin:0 0 10px;font-size:12px}
.okbox{border-left:3px solid var(--pos);background:rgba(46,204,113,.07);
padding:8px 11px;margin:0 0 10px;font-size:12px}
.tabs{display:flex;flex-wrap:wrap;gap:1px;background:var(--line);
margin:0 0 10px}
.tabs button{background:var(--surface);color:var(--ink2);border:0;
padding:6px 12px;cursor:pointer;font:inherit;font-size:12px}
.tabs button.on{background:var(--accent);color:#04070c;font-weight:600}
.count{color:var(--muted);font-size:11px;margin-left:6px}
.empty{color:var(--muted);padding:10px 0;font-size:12px}
"""

#: No framework, no build step, no network. Plain DOM against an object
#: that is already in the page.
_JS = r"""
var B = window.__BUNDLE__ || {};
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;
if(x!==undefined)e.textContent=x;return e;}
function fmt(v){
  if(v===null||v===undefined)return '';
  if(typeof v==='object')return JSON.stringify(v);
  return String(v);
}
function table(rows, cols){
  if(!rows||!rows.length)return el('p','empty','No rows.');
  cols = cols || Object.keys(rows[0]);
  var w=el('div','scroll'),t=el('table'),h=el('tr');
  cols.forEach(function(c){h.appendChild(el('th',null,c));});
  var th=el('thead');th.appendChild(h);t.appendChild(th);
  var tb=el('tbody');
  rows.forEach(function(r){
    var tr=el('tr');
    cols.forEach(function(c){
      var td=el('td','mono',fmt(r[c]));
      if(String(r[c]||'').length>200)td.title=fmt(r[c]);
      tr.appendChild(td);});
    tb.appendChild(tr);});
  t.appendChild(tb);w.appendChild(t);return w;
}

/* ---- logs, filtered in the browser ---- */
function renderLogs(){
  var host=document.getElementById('logrows');
  if(!host)return;
  var lv=document.getElementById('f-level').value;
  var cp=document.getElementById('f-comp').value;
  var q =document.getElementById('f-q').value.toLowerCase();
  var rows=(B.recent_logs||[]).filter(function(r){
    if(lv && r.level!==lv)return false;
    if(cp && r.component!==cp)return false;
    if(q){
      var hay=((r.message||'')+' '+(r.traceback_text||'')+' '+
               (r.context_json||'')+' '+(r.component||'')).toLowerCase();
      if(hay.indexOf(q)<0)return false;
    }
    return true;});
  document.getElementById('logcount').textContent =
    rows.length+' of '+((B.recent_logs||[]).length)+' line(s)';
  host.innerHTML='';
  if(!rows.length){host.appendChild(el('p','empty',
    'Nothing matches. Clear the filters to see everything again.'));return;}
  var w=el('div','scroll'),t=el('table');
  var th=el('thead'),h=el('tr');
  ['when','level','component','message'].forEach(function(c){
    h.appendChild(el('th',null,c));});
  th.appendChild(h);t.appendChild(th);
  var tb=el('tbody');
  rows.forEach(function(r){
    var tr=el('tr');
    tr.appendChild(el('td','mono',r.ts||''));
    var td=el('td');var p=el('span','pill '+(r.level||''),r.level||'');
    td.appendChild(p);tr.appendChild(td);
    tr.appendChild(el('td','mono',r.component||''));
    var m=el('td');m.appendChild(el('div',null,r.message||''));
    if(r.traceback_text){
      var d=el('details');d.appendChild(el('summary',null,'traceback'));
      d.appendChild(el('pre',null,r.traceback_text));m.appendChild(d);}
    if(r.context_json){
      var d2=el('details');d2.appendChild(el('summary',null,'context'));
      d2.appendChild(el('pre',null,r.context_json));m.appendChild(d2);}
    tr.appendChild(m);
    tb.appendChild(tr);});
  t.appendChild(tb);w.appendChild(t);host.appendChild(w);
}

/* ---- one tab per table carried in the bundle ---- */
function renderTables(){
  var host=document.getElementById('tablebody');
  var tabs=document.getElementById('tabletabs');
  if(!host||!tabs)return;
  var rows=B.rows||{};
  var names=Object.keys(rows).sort();
  if(!names.length){
    host.appendChild(el('p','empty',
      'This bundle carries counts only, not rows. Download it again with '+
      'a wider scope to get every row verbatim.'));return;}
  function show(n){
    host.innerHTML='';
    Array.prototype.forEach.call(tabs.children,function(b){
      b.className = (b.dataset.n===n)?'on':'';});
    var tr=(B.rows_truncated||{})[n];
    if(tr){var w=el('div','warnbox');w.textContent=
      'This table was truncated: '+fmt(tr);host.appendChild(w);}
    host.appendChild(table(rows[n]));
  }
  names.forEach(function(n){
    var b=el('button',null,n+' ('+(rows[n]||[]).length+')');
    b.dataset.n=n;b.onclick=function(){show(n);};tabs.appendChild(b);});
  /* OPEN ON SOMETHING WORTH READING. Alphabetical order puts
     adaptive_param_log first, which is almost always empty, so the
     panel greeted every reader with the words "No rows." The biggest
     table is the one most likely to hold the answer. */
  var best=names[0],most=-1;
  names.forEach(function(n){
    var c=(rows[n]||[]).length;
    if(c>most){most=c;best=n;}});
  show(best);
}

document.addEventListener('DOMContentLoaded',function(){
  ['f-level','f-comp'].forEach(function(id){
    var e=document.getElementById(id);if(e)e.onchange=renderLogs;});
  var q=document.getElementById('f-q');if(q)q.oninput=renderLogs;
  renderLogs();renderTables();
});
"""


def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def logs_as_text(bundle: dict) -> str:
    """The log lines as a plain file.

    Deliberately the dullest possible format, one line per record with
    tracebacks indented under their own line: this is the file to grep,
    to tail, and to paste into a message. The HTML reader is for
    browsing; this is for tools.
    """
    out = []
    for row in (bundle.get("recent_logs") or []):
        if not isinstance(row, dict):
            out.append(str(row))
            continue
        out.append(f"{row.get('ts','')} {str(row.get('level','')):8} "
                   f"{row.get('component','')} {row.get('message','')}")
        for field in ("traceback_text", "context_json"):
            blob = row.get(field)
            if blob:
                for line in str(blob).splitlines():
                    out.append(f"    | {line}")
    if not out:
        out.append("No log rows in this bundle.")
    return "\n".join(out) + "\n"


def _tiles(bundle: dict) -> str:
    logs = bundle.get("recent_logs") or []
    counts = {}
    for row in logs:
        if isinstance(row, dict):
            counts[row.get("level", "?")] = counts.get(row.get("level", "?"), 0) + 1
    bad = counts.get("ERROR", 0) + counts.get("CRITICAL", 0)
    rows = bundle.get("rows") or {}
    items = [
        ("Taken", _esc(str(bundle.get("generated_at", ""))[:19]),
         "UTC, when this bundle was made"),
        ("Build", _esc(bundle.get("build_hash", "unknown")),
         "the code that produced it"),
        ("Log lines", f"{len(logs):,}",
         (f"{bad} error(s) or worse" if bad
          else "none at ERROR or above")),
        ("Tables carried", f"{len(rows):,}",
         "every row verbatim" if rows else "counts only, no rows"),
    ]
    cells = "".join(
        f'<div class="tile"><p class="k">{k}</p><p class="v">{v}</p>'
        f'<p class="s">{_esc(s)}</p></div>' for k, v, s in items)
    return f'<div class="tiles">{cells}</div>'


def _scope_box(bundle: dict) -> str:
    scope = _esc(bundle.get("scope", "all"))
    covers = _esc(bundle.get("scope_covers", ""))
    note = _esc(bundle.get("scope_note", ""))
    window = _esc(bundle.get("window_note", ""))
    # WHAT IS NOT IN HERE, said first. A scoped bundle read as a
    # complete one is how a missing table becomes a wild goose chase.
    return (f'<div class="warnbox"><b>Scope: {scope}</b> &mdash; {covers}'
            f'<br>{note}<br>{window}</div>')


def _errors_box(bundle: dict) -> str:
    errs = [r for r in (bundle.get("recent_logs") or [])
            if isinstance(r, dict) and r.get("level") in ("ERROR", "CRITICAL")]
    if not errs:
        return ('<div class="okbox">No line in this bundle is at ERROR or '
                'above. That is not proof nothing is wrong - a fault that '
                'never logged will not appear here - but nothing announced '
                'itself.</div>')
    newest = errs[:6]
    body = "".join(
        f'<div class="mono">{_esc(r.get("ts",""))} '
        f'{_esc(r.get("component",""))} &mdash; {_esc(r.get("message",""))}</div>'
        for r in newest)
    more = (f'<div class="note">and {len(errs) - len(newest)} more '
            "below.</div>" if len(errs) > len(newest) else "")
    return (f'<div class="critbox"><b>{len(errs)} line(s) at ERROR or '
            f"above.</b> Start here.{body}{more}</div>")


def render_bundle_html(bundle: dict) -> str:
    """The whole reader, as one file that opens with a double-click."""
    logs = [r for r in (bundle.get("recent_logs") or []) if isinstance(r, dict)]
    levels = sorted({r.get("level", "") for r in logs if r.get("level")})
    comps = sorted({r.get("component", "") for r in logs if r.get("component")})
    lvl_opts = "".join(f'<option value="{_esc(x)}">{_esc(x)}</option>'
                       for x in levels)
    comp_opts = "".join(f'<option value="{_esc(x)}">{_esc(x)}</option>'
                        for x in comps)

    # </script> INSIDE THE DATA WOULD END THE TAG EARLY and silently
    # truncate the page - a log line quoting HTML is not a hypothetical
    # in a bot that reads filings and news. Escaping the sequence is the
    # standard defence and JSON parses the result identically.
    payload = (json.dumps(bundle, default=str)
               .replace("</", "<\\/")
               .replace(" ", "\\u2028")
               .replace(" ", "\\u2029"))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Catalyst diagnostics &mdash; {_esc(str(bundle.get('generated_at',''))[:19])}</title>
<style>{_CSS}</style></head><body>
<header>
  <h1>Catalyst diagnostic bundle</h1>
  <p class="sub">Open this file in any browser. Everything is inside it &mdash;
  no internet, no install, nothing to run. The raw
  <span class="mono">bundle.json</span> and
  <span class="mono">logs.txt</span> are in this same folder, unchanged,
  for grepping or sending on.</p>
</header>
<main>
<section><h2>At a glance</h2>{_tiles(bundle)}</section>
<section><h2>Read this first</h2><div class="body">
{_scope_box(bundle)}{_errors_box(bundle)}
<p class="note">Credentials are stripped twice before this file is
written &mdash; once where each value is captured, and again over the
whole bundle. Environment variable <i>names</i> are listed; their values
are never collected at all.</p>
</div></section>
<section><h2>Logs</h2>
  <div class="bar">
    <label>level <select id="f-level"><option value="">any</option>
      {lvl_opts}</select></label>
    <label>component <select id="f-comp"><option value="">any</option>
      {comp_opts}</select></label>
    <label>text <input id="f-q" size="34"
      placeholder="message, traceback or context"></label>
    <span class="count" id="logcount"></span>
  </div>
  <div class="body" id="logrows"></div>
</section>
<section><h2>Tables</h2>
  <div class="tabs" id="tabletabs"></div>
  <div class="body" id="tablebody"></div>
</section>
<section><h2>Everything else in the bundle</h2><div class="body">
  <p class="note">Row counts, the funnel, cost figures and the build
  manifest, exactly as recorded.</p>
  <details><summary>show the raw JSON</summary>
    <pre id="raw">{_esc(json.dumps({k: v for k, v in bundle.items()
                                    if k not in ("recent_logs", "rows")},
                                   indent=2, default=str))}</pre></details>
</div></section>
</main>
<script type="application/json" id="bundle-data">{payload}</script>
<script>
window.__BUNDLE__ = JSON.parse(
  document.getElementById('bundle-data').textContent);
{_JS}
</script>
</body></html>
"""
