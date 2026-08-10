"""The first-run setup page.

Self-contained: standard library only, no framework, no templates. That
is deliberate. This page has to work on a machine where nothing has been
configured yet, and it is the one screen whose failure the owner cannot
route around - there is no dashboard to fall back to.

Two ways to use it:

    from catalyst.setup.first_run import SetupApp, serve
    serve(SetupApp())                       # standalone, port 8000

    app = SetupApp(path_prefix="/setup")    # mounted by the dashboard
    response = app.handle("GET", "/setup/")

`SetupApp.handle()` is a pure function of (method, path, body, headers)
-> Response. It opens no sockets, which is why the offline test suite can
exercise every branch of it, and why the stage-6 dashboard can mount it
without adopting an HTTP server it did not choose.

The owner is not a developer. Nothing on this page shows a file path, a
command, or a configuration key, and no value is ever displayed back
once it has been saved.
"""

from __future__ import annotations

import html
import json
import logging
import math
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from catalyst.setup import credentials as creds

_log = logging.getLogger("catalyst.setup.first_run")

DEFAULT_PORT = 8000
DEFAULT_BIND = "0.0.0.0"  # the VPS is IP restricted (BUILD-BRIEF.md)
TOKEN_COOKIE = "catalyst_access"


@dataclass(frozen=True)
class Field:
    """One thing the owner has to paste in, and the plain-English
    explanation of where to find it. The explanations are part of the
    contract - tests assert every one of them renders."""

    name: str
    label: str
    explanation: str
    kind: str = "secret"
    default: str = ""


FIELDS: tuple[Field, ...] = (
    Field(
        name="alpaca_key",
        label="Alpaca key",
        explanation=(
            "Your Alpaca PAPER API key - found in your Alpaca dashboard under "
            "Paper Trading, then API Keys. Press Generate there if you have not "
            "made one yet. It begins with the letters PK. Paper Trading means "
            "practice money, which is all this bot will ever use until you "
            "decide otherwise."
        ),
    ),
    Field(
        name="alpaca_secret",
        label="Alpaca secret",
        explanation=(
            "The secret Alpaca showed you at the same moment as the key above. "
            "Alpaca shows it once and never again, so if you did not copy it, "
            "press Generate for a new pair and paste both of the new values."
        ),
    ),
    Field(
        name="anthropic_key",
        label="Anthropic key",
        explanation=(
            "Your Anthropic API key. This is what lets the bot ask Claude to "
            "research a company before it trades. Get it from the Anthropic "
            "console at console.anthropic.com under API Keys. It begins with "
            "sk-ant- and is quite long."
        ),
    ),
    Field(
        name="anthropic_admin_key",
        label="Anthropic ADMIN key (optional, recommended)",
        explanation=(
            "A second Anthropic key that starts sk-ant-admin. It lets the "
            "bot check its own spending records against the real Anthropic "
            "bill every night, and pause spending if they disagree. It is "
            "only ever used to READ your bill - never to change any limit. "
            "Leave it blank to skip the nightly check. Get it from the "
            "Anthropic console under Organization, then API keys."
        ),
    ),
    Field(
        name="account_mode",
        label="Which account to trade",
        explanation=(
            "Paper means practice: the bot trades a simulated account with "
            "fake money against the real market, and nothing you own is at "
            "risk. Live means real money leaves your Alpaca account when the "
            "bot buys. The bot is built to prove itself on paper first; "
            "switching to live is always your explicit choice here, never "
            "something the bot decides."
        ),
        kind="account_mode",
        default="paper",
    ),
    Field(
        name="monthly_budget_usd",
        label="Your own spending limit (a brake, not a budget)",
        explanation=(
            "The bot has its own ceiling of $5 a month, and this box "
            "CANNOT raise it - a larger number here changes nothing. It "
            "only ever tightens the limit, so enter a smaller figure if "
            "you want the bot to spend less than $5. That $5 ceiling can "
            "rise only out of profit the bot has actually banked, never "
            "out of profit it merely hopes for, and never past $8 without "
            "a human changing the code."
        ),
        kind="number",
        default="5",
    ),
)

_SETTING_FIELDS = {"monthly_budget_usd", "account_mode"}
_OPTIONAL_FIELDS = {"anthropic_admin_key"}   # blank = feature off, never a refusal
_SECRET_FIELD_NAMES = tuple(f.name for f in FIELDS
                            if f.name not in _SETTING_FIELDS
                            and f.name not in _OPTIONAL_FIELDS)


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes
    headers: list[tuple[str, str]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> dict:
        return json.loads(self.text)


def _json(status: int, payload: dict, headers: list[tuple[str, str]] | None = None) -> Response:
    return Response(
        status=status,
        content_type="application/json; charset=utf-8",
        body=json.dumps(payload).encode("utf-8"),
        headers=headers or [],
    )


def _page(status: int, body_html: str, headers: list[tuple[str, str]] | None = None) -> Response:
    return Response(
        status=status,
        content_type="text/html; charset=utf-8",
        body=body_html.encode("utf-8"),
        headers=headers or [],
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

# Owner report 2026-08-10: "the initial setup is a bit hard to see with
# the colours". Root cause, reproduced in a dark-mode browser: the page
# declared `color-scheme: light dark`, so the browser painted a DARK
# background and light default text - while every colour below was a
# hardcoded LIGHT-mode value. The field explanations (#444 on near
# black), the Test buttons (inherited white text on #f3f3f3) and the
# whole privacy note (white on #f6f6f6) were effectively invisible.
#
# A page may not promise a colour scheme it has not actually written.
# Both schemes are now defined explicitly, from the same tokens the
# dashboard uses, so setup and dashboard look like one product.
_STYLE = """
:root {
  color-scheme: light;
  --page:      #f2f2ef;
  --surface:   #fbfbf9;
  --ink:       #0b0b0b;
  --ink-2:     #43423f;
  --muted:     #6b6a65;
  --hairline:  #d6d5cd;
  --field:     #ffffff;
  --focus:     #2a78d6;
  --good:      #0f6b33;
  --good-wash: #eaf6ee;
  --good-ink:  #0d5227;
  --crit:      #b3261e;
  --crit-wash: #fdeeee;
  --crit-ink:  #8c1c16;
  --primary-ink: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page:      #0d0d0d;
    --surface:   #1a1a19;
    --ink:       #ffffff;
    --ink-2:     #d2d1c8;
    --muted:     #a3a29a;
    --hairline:  #383835;
    --field:     #232322;
    --focus:     #6da7ec;
    --good:      #2f9e57;
    --good-wash: #14261a;
    --good-ink:  #8fd7a6;
    --crit:      #e06a63;
    --crit-wash: #2b1615;
    --crit-ink:  #f0a9a4;
    /* Measured: white on the dark-mode green is only 3.42:1, short of
       AA for text this size. Dark ink on the same green is 5.76:1, and
       the button also stands out better against the near-black page
       (5.69:1 vs 2.93:1 for a darker green with white text). */
    --primary-ink: #0b0b0b;
  }
}
* { box-sizing: border-box; }
body { font-family: system-ui, -apple-system, "Segoe UI", Helvetica, sans-serif;
       max-width: 46rem; margin: 0 auto; padding: 2rem 1.25rem 5rem;
       line-height: 1.6; color: var(--ink); background: var(--page);
       -webkit-font-smoothing: antialiased; }
h1 { font-size: 1.55rem; margin-bottom: .3rem; letter-spacing: -.01em; }
.lede { color: var(--ink-2); margin-top: 0; }
fieldset { border: 1px solid var(--hairline); border-radius: 12px;
           padding: 1.1rem 1.2rem .3rem; margin: 1.4rem 0;
           background: var(--surface); }
legend { font-weight: 650; padding: 0 .45rem; font-size: .95rem;
         color: var(--ink); }
label { display: block; font-weight: 600; margin-top: .3rem; }
/* The explanations are the point of this page - they must read as body
   text, not as fine print the eye skips. */
.explain { color: var(--ink-2); font-size: .95rem; margin: .15rem 0 .6rem;
           font-weight: 400; max-width: 62ch; }
input[type=password], input[type=text], input[type=number] {
   width: 100%; padding: .65rem .75rem; font-size: 1rem; border-radius: 9px;
   border: 1px solid var(--hairline); background: var(--field);
   color: var(--ink); }
input:focus-visible, button:focus-visible {
   outline: 3px solid var(--focus); outline-offset: 2px; }
button { font-size: 1rem; padding: .6rem 1.05rem; border-radius: 9px;
         border: 1px solid var(--hairline); background: var(--surface);
         color: var(--ink); cursor: pointer; font-weight: 600;
         margin-bottom: 1rem; }
button:hover { border-color: var(--focus); color: var(--focus); }
button.primary { background: var(--good); color: var(--primary-ink);
                 border-color: var(--good);
                 font-size: 1.05rem; padding: .85rem 1.5rem; }
button.primary:hover { filter: brightness(1.08); color: var(--primary-ink); }
.result { margin: .7rem 0 1rem; padding: .7rem .85rem; border-radius: 9px;
          display: none; white-space: pre-wrap; }
.result.good { display: block; background: var(--good-wash);
               border: 1px solid var(--good); color: var(--good-ink); }
.result.bad  { display: block; background: var(--crit-wash);
               border: 1px solid var(--crit); color: var(--crit-ink); }
.note { background: var(--surface); border: 1px solid var(--hairline);
        border-left: 4px solid var(--muted); padding: .85rem 1rem;
        margin: 1.6rem 0; font-size: .95rem; border-radius: 0 10px 10px 0;
        color: var(--ink-2); }
.show-toggle { font-size: .92rem; color: var(--ink-2); margin: .6rem 0;
               font-weight: 400; }
.show-toggle input { margin-right: .4rem; }
/* One choice per row, each a target you can hit. Run together as
   inline text these two read as a single paragraph - and one of them
   spends real money. */
label.radio { display: flex; gap: .6rem; align-items: flex-start;
              font-weight: 400; border: 1px solid var(--hairline);
              border-radius: 10px; padding: .7rem .8rem; margin: .45rem 0;
              cursor: pointer; background: var(--page); }
label.radio:hover { border-color: var(--focus); }
label.radio input { margin-top: .25rem; flex: none; }
label.radio b { font-weight: 650; }
"""

_SCRIPT = """
function q(id){ return document.getElementById(id); }
function show(id, ok, message){
  var el = q(id);
  el.className = 'result ' + (ok ? 'good' : 'bad');
  el.textContent = message;
}
function post(path, payload){
  return fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                      credentials:'same-origin', body: JSON.stringify(payload)})
    .then(function(r){ return r.json(); })
    .catch(function(e){ return {ok:false, message:
      'The page could not reach the bot. It may still be starting up - '
      + 'wait ten seconds and try again. (' + e + ')'}; });
}
var BOT_CEILING_USD = 5;
function budgetHint(){
  var el = q('monthly_budget_usd'); if(!el) return;
  var hint = q('monthly_budget_usd_hint'); if(!hint) return;
  var v = parseFloat(el.value);
  if (isNaN(v)) { hint.textContent = ''; return; }
  if (v > BOT_CEILING_USD) {
    hint.textContent = 'Note: the bot will still stop at $' + BOT_CEILING_USD
      + ' a month. This box cannot raise the ceiling, only lower it, so '
      + '$' + v + ' has the same effect as $' + BOT_CEILING_USD + '.';
  } else if (v === 0) {
    hint.textContent = 'This stops the bot researching anything at all, so '
      + 'it will never place a trade.';
  } else {
    hint.textContent = 'The bot will stop at $' + v + ' a month.';
  }
}
function testAlpaca(){
  show('alpaca_result', true, 'Checking with Alpaca...');
  post(PREFIX + '/test/alpaca', {alpaca_key: q('alpaca_key').value,
                                 alpaca_secret: q('alpaca_secret').value})
    .then(function(r){ show('alpaca_result', r.ok, r.message); });
}
function testAnthropic(){
  show('anthropic_result', true, 'Checking with Anthropic...');
  post(PREFIX + '/test/anthropic', {anthropic_key: q('anthropic_key').value})
    .then(function(r){ show('anthropic_result', r.ok, r.message); });
}
function saveAll(){
  show('save_result', true, 'Checking both connections, then saving...');
  post(PREFIX + '/save', {
    alpaca_key: q('alpaca_key').value,
    alpaca_secret: q('alpaca_secret').value,
    anthropic_key: q('anthropic_key').value,
    monthly_budget_usd: q('monthly_budget_usd').value
  }).then(function(r){
    show('save_result', r.ok, r.message);
    if (r.ok) { setTimeout(function(){ window.location = PREFIX + '/'; }, 2500); }
  });
}
function toggleShow(cb){
  var t = cb.checked ? 'text' : 'password';
  ['alpaca_key','alpaca_secret','anthropic_key'].forEach(function(id){ q(id).type = t; });
}
"""


def _shell(title: str, inner: str, prefix: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style>"
        f"<script>var PREFIX={json.dumps(prefix)};{_SCRIPT}</script>"
        f"</head><body>{inner}</body></html>\n"
    )


def _field_input(f: Field) -> str:
    if f.kind == "account_mode":
        # Each option is its own row with its own lead-in. Run together
        # as inline text they read as one paragraph, and one of them
        # spends real money.
        return (
            f'<label class="radio"><input type="radio" name="{f.name}" '
            f'value="paper" checked><span><b>Practice account (paper)</b>'
            f" &mdash; fake money, real market. Recommended until the "
            f"record proves itself.</span></label>"
            f'<label class="radio"><input type="radio" name="{f.name}" '
            f'value="live"><span><b>Live account &mdash; REAL MONEY.</b> '
            f"Only choose this deliberately, with live Alpaca keys, once "
            f"the paper record has convinced you.</span></label>")
    if f.kind == "number":
        # Live feedback, because the field silently ignores anything
        # above the ceiling. Owner report 2026-08-10: entered 20, saw
        # $5 everywhere afterwards and had no way to know why. The
        # explanation above says it; this says it at the moment the
        # number is typed, which is when it is actually read.
        return (
            f'<input id="{f.name}" name="{f.name}" type="number" min="0" step="1" '
            f'value="{html.escape(f.default)}" oninput="budgetHint()">'
            f'<p class="explain" id="{f.name}_hint"></p>'
        )
    return (
        f'<input id="{f.name}" name="{f.name}" type="password" autocomplete="off" '
        f'spellcheck="false" placeholder="paste it here">'
    )


def render_setup_page(prefix: str = "") -> str:
    """The form. Every field carries its plain-English explanation; no
    value is ever pre-filled, not even one already saved."""
    by_name = {f.name: f for f in FIELDS}
    blocks = []

    def block(legend: str, names: list[str], extra: str) -> str:
        parts = [f"<fieldset><legend>{html.escape(legend)}</legend>"]
        for name in names:
            f = by_name[name]
            parts.append(f'<label for="{f.name}"><strong>{html.escape(f.label)}</strong></label>')
            parts.append(f'<p class="explain">{html.escape(f.explanation)}</p>')
            parts.append(_field_input(f))
        parts.append(extra)
        parts.append("</fieldset>")
        return "".join(parts)

    blocks.append(block(
        "Your broker (Alpaca)",
        ["alpaca_key", "alpaca_secret"],
        '<label class="show-toggle"><input type="checkbox" onclick="toggleShow(this)"> '
        "Show what I have pasted, so I can check it</label>"
        '<p><button type="button" onclick="testAlpaca()">Test this connection</button></p>'
        '<div id="alpaca_result" class="result"></div>',
    ))
    blocks.append(block(
        "Research (Anthropic)",
        ["anthropic_key", "anthropic_admin_key"],
        '<p><button type="button" onclick="testAnthropic()">Test this connection</button></p>'
        '<div id="anthropic_result" class="result"></div>',
    ))
    blocks.append(block("Practice or real money", ["account_mode"], ""))
    blocks.append(block("Spending limit", ["monthly_budget_usd"], ""))

    inner = (
        "<h1>Welcome - let's get Catalyst running</h1>"
        '<p class="lede">There are three things to paste in below, and each one is '
        "explained. It takes about five minutes. You will not have to do this again.</p>"
        + "".join(blocks)
        + '<p><button class="primary" type="button" onclick="saveAll()">'
          "Save and start trading (practice money only)</button></p>"
          '<div id="save_result" class="result"></div>'
          '<div class="note">Once you press Save, these values are stored where only '
          "Catalyst itself can read them. They are never shown on any screen again, "
          "never written into any report you might send for help, and never appear in "
          "the bot's own records. If you ever need to change one, come back to this "
          "page and paste a new value over it.</div>"
          "<noscript><p>This page needs scripting switched on in your browser to test "
          "the connections. If you cannot switch it on, tell whoever set this up.</p>"
          "</noscript>"
    )
    return _shell("Catalyst setup", inner, prefix)


def render_configured_page(prefix: str = "") -> str:
    inner = (
        "<h1>Catalyst is set up</h1>"
        '<p class="lede">Your details are saved and the bot is running. There is '
        "nothing more to do here.</p>"
        '<div class="note">Your keys are not shown on this page, and never will be. '
        "That is on purpose: anything you can read on a screen can end up in a "
        "screenshot. If a key stops working, or you replace one at Alpaca or "
        "Anthropic, use the button below to paste the new value in.</div>"
        f'<p><a href="{html.escape(prefix)}/?replace=1"><button type="button">'
        "Replace my keys</button></a></p>"
    )
    return _shell("Catalyst setup", inner, prefix)


def render_locked_page(prefix: str = "") -> str:
    inner = (
        "<h1>This page needs your access code</h1>"
        '<p class="lede">Open the link exactly as it was printed on the screen when '
        "Catalyst was installed - it ends with a long code after "
        "<code>?code=</code>. That code is what keeps other people out.</p>"
        '<div class="note">Lost the link? Whoever installed Catalyst can print it '
        "again from the machine it runs on.</div>"
    )
    return _shell("Catalyst setup", inner, prefix)


# --------------------------------------------------------------------------
# The application
# --------------------------------------------------------------------------


class SetupApp:
    """Routing and behaviour for the setup page.

    The two connection testers are injected so the offline suite can
    drive every success and failure branch without a socket; in
    production they default to the real ones in `credentials`.
    """

    def __init__(
        self,
        *,
        credentials_path: str | None = None,
        path_prefix: str = "",
        on_saved: Callable[[], None] | None = None,
        alpaca_tester: Callable[..., tuple[bool, str]] | None = None,
        anthropic_tester: Callable[..., tuple[bool, str]] | None = None,
        admin_tester: Callable[..., tuple[bool, str]] | None = None,
        require_token: bool = True,
    ) -> None:
        self.credentials_path = credentials_path
        self.path_prefix = path_prefix.rstrip("/")
        self.on_saved = on_saved
        self.alpaca_tester = alpaca_tester or creds.test_alpaca
        self.anthropic_tester = anthropic_tester or creds.test_anthropic
        self.admin_tester = admin_tester or creds.test_admin_key
        self.require_token = require_token

    # -- helpers ---------------------------------------------------------

    def _stored_token(self) -> str:
        try:
            return creds.load_credentials(self.credentials_path).dashboard_token
        except creds.CredentialError:
            return ""

    def _is_configured(self) -> bool:
        return creds.credentials_exist(self.credentials_path)

    def _authorized(self, query: dict[str, list[str]], headers: dict[str, str]) -> bool:
        expected = self._stored_token()
        if not expected:
            # No access code exists yet, so there is nothing to check
            # against and refusing would lock the owner out of their own
            # machine. install.sh always creates one before the service
            # starts, so this is the developer-laptop case.
            return True
        if not self.require_token:
            return True
        import secrets as _secrets

        offered = []
        for key in ("code", "token"):
            offered.extend(query.get(key, []))
        header_token = headers.get("x-dashboard-token") or headers.get("x-access-code")
        if header_token:
            offered.append(header_token)
        cookie = headers.get("cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == TOKEN_COOKIE:
                offered.append(urllib.parse.unquote(value))
        return any(_secrets.compare_digest(o, expected) for o in offered if o)

    def _cookie_header(self, query: dict[str, list[str]]) -> list[tuple[str, str]]:
        """Remember a code that arrived in the link, so the owner is not
        asked for it on every request and so it stops appearing in the
        address bar of later pages."""
        for key in ("code", "token"):
            if query.get(key):
                value = urllib.parse.quote(query[key][0])
                return [(
                    "Set-Cookie",
                    f"{TOKEN_COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000",
                )]
        return []

    @staticmethod
    def _parse_body(body: bytes, headers: dict[str, str]) -> dict[str, str]:
        text = (body or b"").decode("utf-8", "replace")
        content_type = headers.get("content-type", "")
        if "json" in content_type or text.strip().startswith("{"):
            try:
                parsed = json.loads(text or "{}")
            except json.JSONDecodeError:
                return {}
            return {k: ("" if v is None else str(v)) for k, v in parsed.items()} \
                if isinstance(parsed, dict) else {}
        return {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}

    def _route(self, path: str) -> str:
        path = urllib.parse.urlsplit(path).path
        if self.path_prefix and path.startswith(self.path_prefix):
            path = path[len(self.path_prefix):]
        return path or "/"

    # -- the entry point --------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> Response:
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        route = self._route(path)
        method = method.upper()

        # Health is deliberately unauthenticated and deliberately says
        # nothing sensitive: install.sh polls it to prove the service came
        # up, before any access code has been typed anywhere.
        if route == "/health":
            return _json(200, {
                "status": "configured" if self._is_configured() else "awaiting_setup",
                "setup_required": not self._is_configured(),
            })

        if not self._authorized(query, headers):
            return _page(403, render_locked_page(self.path_prefix))

        cookie = self._cookie_header(query)

        if route == "/" and method == "GET":
            if self._is_configured() and not query.get("replace"):
                return _page(200, render_configured_page(self.path_prefix), cookie)
            return _page(200, render_setup_page(self.path_prefix), cookie)

        if route == "/test/alpaca" and method == "POST":
            data = self._parse_body(body, headers)
            mode = (data.get("account_mode") or "paper").strip().lower()
            kwargs = ({"base_url": creds.ALPACA_LIVE_BASE_URL}
                      if mode == "live" else {})
            ok, message = self.alpaca_tester(data.get("alpaca_key", ""),
                                             data.get("alpaca_secret", ""),
                                             **kwargs)
            return _json(200, {"ok": bool(ok), "message": message}, cookie)

        if route == "/test/anthropic" and method == "POST":
            data = self._parse_body(body, headers)
            ok, message = self.anthropic_tester(data.get("anthropic_key", ""))
            return _json(200, {"ok": bool(ok), "message": message}, cookie)

        if route == "/save" and method == "POST":
            return self._save(self._parse_body(body, headers), cookie)

        return _page(404, _shell(
            "Catalyst setup",
            "<h1>There is nothing at this address</h1>"
            '<p class="lede">Go back to the link you were given when Catalyst was '
            "installed.</p>",
            self.path_prefix,
        ))

    def _save(self, data: dict[str, str], cookie: list[tuple[str, str]]) -> Response:
        alpaca_key = (data.get("alpaca_key") or "").strip()
        alpaca_secret = (data.get("alpaca_secret") or "").strip()
        anthropic_key = (data.get("anthropic_key") or "").strip()

        blank = [
            f.label for f in FIELDS
            if f.name in _SECRET_FIELD_NAMES and not (data.get(f.name) or "").strip()
        ]
        if blank:
            return _json(200, {
                "ok": False,
                "message": ("Still empty: " + ", ".join(blank) +
                            ". Paste a value into each box, then press Save again."),
            }, cookie)

        save_mode = (data.get("account_mode") or "paper").strip().lower()
        mode_kwargs = ({"base_url": creds.ALPACA_LIVE_BASE_URL}
                       if save_mode == "live" else {})
        ok, message = self.alpaca_tester(alpaca_key, alpaca_secret,
                                         **mode_kwargs)
        if not ok:
            return _json(200, {
                "ok": False,
                "message": ("Nothing was saved, because the Alpaca details did not "
                            "work. " + message),
            }, cookie)

        ok, message = self.anthropic_tester(anthropic_key)
        if not ok:
            return _json(200, {
                "ok": False,
                "message": ("Nothing was saved, because the Anthropic key did not "
                            "work. " + message),
            }, cookie)

        budget_raw = (data.get("monthly_budget_usd") or "5").strip()
        try:
            budget = float(budget_raw)
            # float() accepts "nan" and "inf". NaN then passes `< 0`
            # because every comparison with NaN is False, so it would be
            # stored as a spending limit that no comparison can ever
            # exceed (stage-8 stress).
            if not math.isfinite(budget) or budget < 0:
                raise ValueError
        except ValueError:
            return _json(200, {
                "ok": False,
                "message": ("Nothing was saved. The monthly research budget must be a "
                            f"plain number of dollars between 0 and 100 - "
                            f"\"{html.escape(budget_raw)}\" is not one. Try 5."),
            }, cookie)

        account_mode = (data.get("account_mode") or "paper").strip().lower()
        if account_mode not in ("paper", "live"):
            return _json(200, {
                "ok": False,
                "message": ("Nothing was saved. The account choice must be "
                            "either the practice account or the live one."),
            }, cookie)

        admin_raw = (data.get("anthropic_admin_key") or "").strip()
        if admin_raw:
            ok, message = self.admin_tester(admin_raw)
            if not ok:
                return _json(200, {
                    "ok": False,
                    "message": ("Nothing was saved, because the Anthropic "
                                "ADMIN key did not work. Leave it blank to "
                                "skip the nightly bill check, or fix it. "
                                + message),
                }, cookie)

        try:
            creds.save_credentials(
                alpaca_key,
                alpaca_secret,
                anthropic_key,
                None,  # keep the access code this machine already has
                anthropic_admin_key=admin_raw,
                settings={"monthly_budget_usd": budget,
                          "account_mode": account_mode},
                path=self.credentials_path,
            )
        except creds.CredentialError as exc:
            # Already redacted at the point of capture inside credentials.
            return _json(200, {
                "ok": False,
                "message": ("Nothing was saved. " + str(exc)),
            }, cookie)

        if self.on_saved is not None:
            try:
                self.on_saved()
            except Exception:  # noqa: BLE001 - a callback must not break setup
                _log.exception("post-setup callback failed")

        return _json(200, {
            "ok": True,
            "message": ("All saved, and both connections worked. Catalyst is starting "
                        "now and will begin looking for trades on the next market "
                        "session. You can close this page."),
        }, cookie)


# --------------------------------------------------------------------------
# Standalone server (used before the stage-6 dashboard exists)
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "catalyst-setup"
    sys_version = ""

    @property
    def app(self) -> SetupApp:
        return self.server.app  # type: ignore[attr-defined]

    def _respond(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            response = self.app.handle(method, self.path, body, dict(self.headers))
        except Exception:  # noqa: BLE001 - never take the service down over a request
            _log.exception("setup request failed")
            response = _page(500, _shell(
                "Catalyst setup",
                "<h1>Something went wrong inside Catalyst</h1>"
                '<p class="lede">This is a fault in the bot, not something you did. '
                "Reload the page; if it keeps happening, whoever set this up can see "
                "the details in the bot's own log.</p>", ""))
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response.body)

    def do_GET(self) -> None:  # noqa: N802
        self._respond("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._respond("POST")

    def log_message(self, fmt: str, *args) -> None:
        # The access code arrives as a query parameter, so the default
        # access log would write it to the journal on every request. Log
        # the path only, with the query stripped, through the redactor.
        try:
            path = urllib.parse.urlsplit(self.path).path
        except Exception:  # noqa: BLE001
            path = "?"
        _log.info("%s %s", self.command, creds.redact(path))


def make_server(app: SetupApp | None = None, host: str = DEFAULT_BIND,
                port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.app = app or SetupApp()  # type: ignore[attr-defined]
    return server


def serve(app: SetupApp | None = None, host: str = DEFAULT_BIND,
          port: int = DEFAULT_PORT) -> None:
    server = make_server(app, host, port)
    _log.info("setup page listening on %s:%s", host, port)
    server.serve_forever()
