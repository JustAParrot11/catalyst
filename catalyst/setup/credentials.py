"""The credential store.

One file, mode 0600, owned by the service user. Written by the setup
form (`catalyst.setup.first_run`), read by the broker, the research
boundary and the dashboard. Never written to the repository, never
logged, never shown again once saved, never in a diagnostic bundle.

Redaction is at the point of capture. Every public entry point below
registers the secret values it was handed with the redactor as its first
statement, and every one of them is wrapped in `_scrubbed`, which
converts any exception into a `CredentialError` whose message has been
passed through `redact()` and whose `__cause__` is suppressed. Chaining
with `raise ... from e` would have carried the original, unredacted
message out inside the traceback, so it is deliberately not done.

Environment overrides (all optional; the installer sets them in the
systemd unit):

    CATALYST_CREDENTIALS   path to the credentials file
                           (default /etc/catalyst/credentials.json)
    CATALYST_SERVICE_USER  the user that must own it (default "catalyst")
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

DEFAULT_CREDENTIALS_PATH = "/etc/catalyst/credentials.json"
DEFAULT_SERVICE_USER = "catalyst"

# Paper only, always. TRAPS.md/BUILD-BRIEF.md: paper account until
# performance is proven, so the connection test has no live URL to
# accidentally point at.
ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_BASE_URL = "https://api.alpaca.markets"
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

FILE_FORMAT_VERSION = 1
REDACTED = "***"

# A transport is any callable (url, headers) -> (status_code, body_text).
# The connection tests take one so the suite can exercise every branch
# offline; the default one below is the only thing that touches a socket.
Transport = Callable[[str, "dict[str, str]"], "tuple[int, str]"]

_log = logging.getLogger("catalyst.setup.credentials")


class CredentialError(RuntimeError):
    """Anything that went wrong handling credentials, with every secret
    value already replaced by ``***``. This is the only exception type
    this module raises."""


# --------------------------------------------------------------------------
# Redaction - at capture, not on the way out
# --------------------------------------------------------------------------

# Values handed to this module at runtime. Short strings are ignored: a
# four-character "secret" would redact half of every English sentence.
_MIN_SECRET_LEN = 8
_KNOWN_SECRETS: set[str] = set()

# Belt and braces for values that were never registered - a key pasted
# into the wrong field, or one read from somewhere that forgot to call
# remember_secret(). These patterns match the documented key shapes.
_SECRET_PATTERNS = (
    re.compile(r"PK[A-Z0-9]{8,}"),                 # Alpaca key id
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),      # Anthropic key
    re.compile(r"CK[A-Z0-9]{8,}"),                 # Alpaca live key id
)


def remember_secret(value: Any) -> None:
    """Register a value so it can never appear in a message from here on.

    Called at capture: the first statement of every function that
    receives a secret."""
    if isinstance(value, str) and len(value.strip()) >= _MIN_SECRET_LEN:
        _KNOWN_SECRETS.add(value.strip())


def redact(text: Any) -> str:
    """Replace every known or key-shaped secret in `text` with ``***``."""
    out = text if isinstance(text, str) else str(text)
    # Longest first: a secret that contains another as a substring must
    # not be half-replaced, leaving the tail exposed.
    for value in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        if value in out:
            out = out.replace(value, REDACTED)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def _is_exc_info(value) -> bool:
    """A real (type, value, tb) triple. `exc_info=True` is normally
    resolved before the record exists, but a hand-built LogRecord can
    still carry the bare flag - and format_exception(*True) would drop
    the whole record."""
    return isinstance(value, tuple) and len(value) == 3 and value[0] is not None


def _redacted_value(value):
    """Redact one log argument, preserving its type unless redaction had
    something to remove. Numbers pass through untouched so %d/%f keep
    working; anything else is judged on the string that would reach the
    log."""
    if isinstance(value, str):
        return redact(value)
    if value is None or isinstance(value, (int, float, complex)):
        return value
    try:
        rendered = str(value)
    except Exception:            # a __str__ that raises is the caller's
        return value             # problem, not a leak this filter can fix
    cleaned = redact(rendered)
    return cleaned if cleaned != rendered else value


class RedactingFilter(logging.Filter):
    """Attach to any logger and no secret can pass through it.

    The service installs this on the root logger at start-up
    (`catalyst.orchestrator.scheduler`), so a well-meaning
    `log.info("calling %s with %s", url, key)` written anywhere in the
    system still cannot leak.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        # Numbers keep their TYPE so %d/%f keep working anywhere in the
        # process (this filter sits on the ROOT logger; coercing every
        # arg to str broke unrelated trading code during the stage-5/7
        # merge dry-run). Everything else is redacted through its
        # rendered form, because that rendered form is what reaches the
        # log: bytes, a dict, or any object whose __str__ carries a key
        # (stage-8 stress: `log.info("payload %s", (KEY, 1))` leaked).
        # And a logging filter must never raise into trading code: on
        # any internal error the record is DROPPED, not passed through
        # unredacted - fail closed for secrecy, open for logging.
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            else:
                record.msg = _redacted_value(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: _redacted_value(v)
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(_redacted_value(a)
                                        for a in record.args)
            # exc_info is turned into text by the HANDLER's formatter,
            # which runs AFTER every filter - so redacting exc_text alone
            # never saw a traceback at all, and every `log.exception(...)`
            # whose exception message held a key went to the journal in
            # clear (stage-8 stress). Formatting it here, into the field
            # the formatter reuses, is what closes that (Formatter.format
            # honours a pre-set record.exc_text).
            if record.exc_text is None and _is_exc_info(record.exc_info):
                record.exc_text = "".join(
                    traceback.format_exception(*record.exc_info)).rstrip()
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
            return True
        except Exception:
            return False


def install_redacting_filter(logger: logging.Logger | None = None) -> None:
    """Install `RedactingFilter` on a logger and all of its handlers."""
    target = logger or logging.getLogger()
    filt = RedactingFilter()
    if not any(isinstance(f, RedactingFilter) for f in target.filters):
        target.addFilter(filt)
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())


def _scrubbed(fn):
    """Wrap a public entry point so no exception can carry a secret out.

    `from None` is load-bearing: exception chaining would print the
    original, unredacted message under "The above exception was the
    direct cause of...".
    """

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except CredentialError as exc:
            raise CredentialError(redact(str(exc))) from None
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            raise CredentialError(
                redact(f"{type(exc).__name__}: {exc}")
            ) from None

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__wrapped__ = fn
    return wrapper


# --------------------------------------------------------------------------
# Where the file lives, and who owns it
# --------------------------------------------------------------------------


def credentials_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get("CATALYST_CREDENTIALS", DEFAULT_CREDENTIALS_PATH))


def service_user() -> str:
    return os.environ.get("CATALYST_SERVICE_USER", DEFAULT_SERVICE_USER)


def _service_user_ids() -> tuple[int, int] | None:
    """(uid, gid) of the service user, or None if we are not root or the
    user does not exist. Not being able to chown is not an error - on a
    developer laptop the file is simply owned by whoever wrote it."""
    if os.geteuid() != 0:
        return None
    try:
        import pwd

        entry = pwd.getpwnam(service_user())
    except (ImportError, KeyError):
        return None
    return entry.pw_uid, entry.pw_gid


def _write_private_file(path: Path, text: str) -> None:
    """Write atomically at mode 0600, owned by the service user.

    Atomic because a half-written credentials file read by the service
    at exactly the wrong moment looks like a corrupt install; the
    replace is a single rename inside the same directory.
    """
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
        ids = _service_user_ids()
        if ids:
            os.chown(parent, *ids)

    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".credentials-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        ids = _service_user_ids()
        if ids:
            os.fchown(fd, *ids)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        if ids:
            os.chown(path, *ids)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# The record itself
# --------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class Credentials:
    """Loaded credentials. Its repr is redacted, so a stray print(),
    an f-string in a log line, or a pytest assertion dump cannot spill
    the values."""

    alpaca_key: str
    alpaca_secret: str
    anthropic_key: str
    dashboard_token: str
    settings: dict = field(default_factory=dict)
    saved_at: str = ""
    anthropic_admin_key: str = ""       # OPTIONAL: read-only Cost API
                                        # reconciliation; blank = the
                                        # nightly bill check is off

    def __repr__(self) -> str:
        return (
            f"Credentials(alpaca_key='{REDACTED}', alpaca_secret='{REDACTED}', "
            f"anthropic_key='{REDACTED}', dashboard_token='{REDACTED}', "
            f"anthropic_admin_key='{REDACTED}', "
            f"settings={self.settings!r}, saved_at={self.saved_at!r})"
        )

    __str__ = __repr__

    def status(self) -> dict[str, bool]:
        """What the dashboard is allowed to show: whether each credential
        is set, never any part of its value."""
        return {
            "alpaca_key": bool(self.alpaca_key),
            "alpaca_secret": bool(self.alpaca_secret),
            "anthropic_key": bool(self.anthropic_key),
            "dashboard_token": bool(self.dashboard_token),
            "anthropic_admin_key": bool(self.anthropic_admin_key),
        }


_SECRET_FIELDS = ("alpaca_key", "alpaca_secret", "anthropic_key",
                  "dashboard_token", "anthropic_admin_key")


def _read_raw(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # Deliberately does not echo the file contents: they are the
        # secrets. Report position only.
        raise CredentialError(
            "The saved settings file could not be read (it is not valid "
            f"JSON at line {exc.lineno}, column {exc.colno}). "
            "Re-enter the details on the setup page to rewrite it."
        ) from None
    if not isinstance(data, dict):
        raise CredentialError(
            "The saved settings file does not have the expected shape. "
            "Re-enter the details on the setup page to rewrite it."
        )
    for name in _SECRET_FIELDS:
        remember_secret(data.get(name))
    return data


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


@_scrubbed
def save_credentials(
    alpaca_key: str,
    alpaca_secret: str,
    anthropic_key: str,
    dashboard_token: str | None = None,
    *,
    anthropic_admin_key: str | None = None,
    settings: dict | None = None,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Write the credentials file. Returns the path it wrote.

    `dashboard_token=None` means "keep the access code this machine
    already has, or make one" - the installer generates it before the
    owner ever sees the setup page, so the form never has to ask for it.

    Existing settings are merged, not replaced: saving new API keys must
    not silently reset a budget the owner set earlier.
    """
    for value in (alpaca_key, alpaca_secret, anthropic_key, dashboard_token,
                  anthropic_admin_key):
        remember_secret(value)

    alpaca_key = (alpaca_key or "").strip()
    alpaca_secret = (alpaca_secret or "").strip()
    anthropic_key = (anthropic_key or "").strip()

    missing = [
        label
        for label, value in (
            ("Alpaca API key ID", alpaca_key),
            ("Alpaca secret key", alpaca_secret),
            ("Anthropic API key", anthropic_key),
        )
        if not value
    ]
    if missing:
        raise CredentialError(
            "These are still blank: " + ", ".join(missing) + ". "
            "Fill them in on the setup page and press Save again."
        )

    target = credentials_path(path)
    existing: dict = {}
    if target.exists():
        try:
            existing = _read_raw(target)
        except CredentialError:
            existing = {}

    token = (dashboard_token or "").strip() or str(existing.get("dashboard_token") or "").strip()
    if not token:
        token = generate_dashboard_token()
    remember_secret(token)

    # None keeps an existing admin key; an explicit value replaces it
    admin = ((anthropic_admin_key or "").strip()
             if anthropic_admin_key is not None
             else str(existing.get("anthropic_admin_key") or "").strip())

    merged_settings = dict(existing.get("settings") or {})
    merged_settings.update(settings or {})

    from datetime import datetime, timezone

    record = {
        "version": FILE_FORMAT_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "alpaca_key": alpaca_key,
        "alpaca_secret": alpaca_secret,
        "anthropic_key": anthropic_key,
        "anthropic_admin_key": admin,
        "dashboard_token": token,
        "settings": merged_settings,
    }
    _write_private_file(target, json.dumps(record, indent=2, sort_keys=True) + "\n")
    _log.info("Credentials saved. Values are not logged.")
    return target


@_scrubbed
def load_credentials(path: str | os.PathLike[str] | None = None) -> Credentials:
    """Read the credentials file. Raises `CredentialError` (already
    redacted) if it is missing or unreadable."""
    target = credentials_path(path)
    if not target.exists():
        raise CredentialError(
            "Catalyst has not been set up yet - open the setup page in a "
            "browser and enter your Alpaca and Anthropic details."
        )
    try:
        data = _read_raw(target)
    except PermissionError:
        raise CredentialError(
            "Catalyst is not allowed to read its own saved settings. "
            f"On the server, run:  sudo chown {service_user()} {target}  "
            f"&& sudo chmod 600 {target}"
        ) from None

    creds = Credentials(
        alpaca_key=str(data.get("alpaca_key") or ""),
        alpaca_secret=str(data.get("alpaca_secret") or ""),
        anthropic_key=str(data.get("anthropic_key") or ""),
        dashboard_token=str(data.get("dashboard_token") or ""),
        settings=dict(data.get("settings") or {}),
        saved_at=str(data.get("saved_at") or ""),
        anthropic_admin_key=str(data.get("anthropic_admin_key") or ""),
    )
    for name in _SECRET_FIELDS:
        remember_secret(getattr(creds, name))
    return creds


def credentials_exist(path: str | os.PathLike[str] | None = None) -> bool:
    """True only when setup is genuinely complete.

    Never raises: a missing file, a corrupt file, a permissions problem
    and a file that only carries the installer-generated access code all
    mean the same thing to the caller - the owner still has to complete
    the setup form.
    """
    try:
        target = credentials_path(path)
        if not target.exists() or target.stat().st_size == 0:
            return False
        data = _read_raw(target)
    except Exception:  # noqa: BLE001 - "not set up" is the only answer here
        return False
    return all(str(data.get(name) or "").strip() for name in
               ("alpaca_key", "alpaca_secret", "anthropic_key"))


def generate_dashboard_token() -> str:
    """A fresh access code for the dashboard. 32 URL-safe characters."""
    return secrets.token_urlsafe(24)


@_scrubbed
def ensure_dashboard_token(path: str | os.PathLike[str] | None = None) -> str:
    """Return this machine's dashboard access code, creating the file
    with just that code in it if setup has not happened yet.

    The installer calls this and prints the code in the "what to do next"
    block, so the owner is never asked to invent one and the setup page
    is protected from the very first request.
    """
    target = credentials_path(path)
    existing: dict = {}
    if target.exists():
        try:
            existing = _read_raw(target)
        except CredentialError:
            existing = {}
    token = str(existing.get("dashboard_token") or "").strip()
    if token:
        remember_secret(token)
        return token

    token = generate_dashboard_token()
    remember_secret(token)
    existing.setdefault("version", FILE_FORMAT_VERSION)
    existing["dashboard_token"] = token
    existing.setdefault("settings", {})
    _write_private_file(target, json.dumps(existing, indent=2, sort_keys=True) + "\n")
    return token


# --------------------------------------------------------------------------
# Connection tests - "does this key actually work", answered honestly
# --------------------------------------------------------------------------


def _default_transport(url: str, headers: dict[str, str], timeout: float = 15.0) -> tuple[int, str]:
    """The only socket-touching code in this module. Returns
    (status, body) for HTTP errors too - the body of a 403 is the actual
    reason, and the owner needs to see it."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return exc.code, body


def _upstream_detail(status: int, body: str) -> str:
    """The upstream response, printed beside the failure. CLAUDE.md house
    rule 3: every zero gets its raw upstream response printed beside it -
    "no data" and "the query is broken" look identical otherwise."""
    body = (body or "").strip()
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            for key in ("message", "error", "detail"):
                value = parsed.get(key)
                if isinstance(value, str) and value:
                    return value
                if isinstance(value, dict) and isinstance(value.get("message"), str):
                    return value["message"]
    except (json.JSONDecodeError, TypeError):
        pass
    if not body:
        return f"(the server sent HTTP {status} with an empty body)"
    return body[:400]


@_scrubbed
def test_alpaca(
    key: str,
    secret: str,
    *,
    transport: Transport | None = None,
    base_url: str | None = None,
) -> tuple[bool, str]:
    """Check an Alpaca paper key pair against GET /v2/account.

    Returns (ok, message). The message is written for someone who has
    never used a terminal, and on failure it quotes what Alpaca actually
    said rather than a summary of it.
    """
    remember_secret(key)
    remember_secret(secret)
    key = (key or "").strip()
    secret = (secret or "").strip()
    if not key or not secret:
        return False, ("Enter both the Alpaca key ID and the Alpaca secret key, "
                       "then press Test again.")

    send = transport or _default_transport
    url = f"{(base_url or ALPACA_PAPER_BASE_URL).rstrip('/')}/v2/account"
    headers = {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "accept": "application/json",
    }
    try:
        status, body = send(url, headers)
    except Exception as exc:  # noqa: BLE001 - network faults are a normal answer here
        return False, redact(
            "Could not reach Alpaca at all. The exact error was: "
            f"{type(exc).__name__}: {exc}. "
            "This usually means the server has no internet connection, "
            "rather than anything wrong with your keys."
        )

    if status == 200:
        try:
            account = json.loads(body)
        except json.JSONDecodeError:
            return False, redact(
                "Alpaca replied, but not with account details. It sent: "
                f"{body[:400]}"
            )
        # TRAPS.md: pattern_day_trader / daytrade_count / daytrading_buying_power
        # were removed from the API in July 2026. Read buying_power only.
        acct_status = account.get("status", "unknown")
        buying_power = account.get("buying_power", "unknown")
        equity = account.get("equity", "unknown")
        return True, redact(
            f"Connected to your Alpaca paper account. Its status is "
            f"{acct_status}, the cash available to trade with is "
            f"${buying_power}, and the account is worth ${equity}."
        )

    detail = _upstream_detail(status, body)
    if status in (401, 403):
        return False, redact(
            f"Alpaca refused these keys (error {status}). Alpaca said: "
            f'"{detail}". The usual cause is using keys from the Live '
            "Trading section instead of Paper Trading, or a key and "
            "secret from two different pairs - generate a fresh pair "
            "under Paper Trading and paste both new values."
        )
    return False, redact(
        f"Alpaca replied with error {status}. Alpaca said: \"{detail}\". "
        "If that mentions maintenance or rate limits, wait a minute and "
        "press Test again."
    )


@_scrubbed
def test_anthropic(
    key: str,
    *,
    transport: Transport | None = None,
    base_url: str | None = None,
) -> tuple[bool, str]:
    """Check an Anthropic key with exactly one request to the models
    endpoint (`GET /v1/models?limit=1`).

    The models endpoint is used rather than a messages call because it
    proves the key works without spending anything - the cost governor's
    $5/month cap should not be nibbled at by setup (BUILD-BRIEF.md).
    """
    remember_secret(key)
    key = (key or "").strip()
    if not key:
        return False, "Enter your Anthropic API key, then press Test again."

    send = transport or _default_transport
    url = f"{(base_url or ANTHROPIC_BASE_URL).rstrip('/')}/v1/models?limit=1"
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "accept": "application/json",
    }
    try:
        status, body = send(url, headers)
    except Exception as exc:  # noqa: BLE001 - network faults are a normal answer here
        return False, redact(
            "Could not reach Anthropic at all. The exact error was: "
            f"{type(exc).__name__}: {exc}. "
            "This usually means the server has no internet connection, "
            "rather than anything wrong with your key."
        )

    if status == 200:
        try:
            payload = json.loads(body)
            models = payload.get("data") or []
            example = models[0].get("id") if models and isinstance(models[0], dict) else None
        except (json.JSONDecodeError, AttributeError, TypeError):
            models, example = [], None
        if example:
            return True, redact(
                f"Connected to Anthropic. Your key works - it can see the "
                f"{example} model. This check cost nothing."
            )
        return True, redact(
            "Connected to Anthropic and your key was accepted, but the "
            f"reply listed no models. Anthropic sent: {body[:400]}"
        )

    detail = _upstream_detail(status, body)
    if status in (401, 403):
        return False, redact(
            f"Anthropic refused this key (error {status}). Anthropic said: "
            f'"{detail}". Check you pasted the whole key - they begin with '
            "sk-ant- and are long - and that it has not been revoked."
        )
    if status == 429:
        return False, redact(
            f'Anthropic is rate limiting this key. It said: "{detail}". '
            "Wait a minute and press Test again."
        )
    return False, redact(
        f"Anthropic replied with error {status}. It said: \"{detail}\"."
    )


def _cli(argv: list[str] | None = None) -> int:
    """Tiny CLI used by install.sh. `--ensure-dashboard-token` prints the
    access code and nothing else, so the installer can put it in the
    link it shows the owner."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--ensure-dashboard-token":
        print(ensure_dashboard_token())
        return 0
    if args and args[0] == "--status":
        print("configured" if credentials_exist() else "awaiting-setup")
        return 0
    print("usage: python -m catalyst.setup.credentials "
          "[--ensure-dashboard-token|--status]")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())


def test_admin_key(key: str, *, transport: "Transport | None" = None,
                   ) -> tuple[bool, str]:
    """Read-only ping of the Cost API (one bucket). Never calls anything
    that could modify limits or settings."""
    remember_secret(key)
    key = (key or "").strip()
    if not key:
        return False, ("Enter the Anthropic ADMIN key (it starts sk-ant-admin) "
                       "then press Test again.")
    # NOT refused on its prefix. Pasting the ordinary API key here is the
    # obvious mistake and the prefix is how you spot it, but refusing on
    # the prefix would lock the owner out the day Anthropic changes the
    # format. So the key is always tried, and the prefix only shapes the
    # explanation when the API itself rejects it.
    looks_like_admin = key.startswith("sk-ant-admin")
    import httpx as _httpx

    # A window that MOVES with the calendar. A fixed one passes for as
    # long as it happens to sit in the past and then quietly stops
    # meaning anything. Yesterday to today is always a closed day.
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    today = _dt.now(_tz.utc).date()
    try:
        with _httpx.Client(transport=transport, timeout=20.0) as client:
            resp = client.get(
                "https://api.anthropic.com/v1/organizations/cost_report",
                headers={"x-api-key": key,
                         "anthropic-version": "2023-06-01"},
                params={"starting_at": f"{today - _td(days=1)}T00:00:00Z",
                        "ending_at": f"{today}T00:00:00Z", "limit": 1})
    except Exception as exc:  # noqa: BLE001
        return False, redact(f"Could not reach the Anthropic Cost API: {exc}")
    if resp.status_code == 200:
        return True, ("The admin key works: the nightly bill check can read "
                      "your organization's real API costs. Anthropic reports "
                      "whole days only, so today's spend does not appear "
                      "until the day closes - a fresh account reading zero "
                      "is normal, not broken.")
    if resp.status_code in (401, 403):
        why = ("This key does not start sk-ant-admin, so it is most likely "
               "the ordinary API key - the bot's thinking key cannot read a "
               "bill. Make an admin key in the Anthropic console under "
               "Settings, then Admin keys; only an organisation owner can. "
               if not looks_like_admin else
               "The key has the right shape, so this is more likely a "
               "permissions or organisation problem than a typo. ")
        return False, redact(
            f"Anthropic refused this key (error {resp.status_code}). {why}"
            f"It said: {resp.text[:300]}")
    return False, redact(
        f"Anthropic refused this admin key (error {resp.status_code}). "
        f"It said: {resp.text[:300]}")
