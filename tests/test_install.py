"""Stage-7 install and setup tests.

Fully offline, like the rest of the suite: the two connection testers are
driven through an injected transport, never a socket (conftest.py blocks
those anyway), and the shell scripts are checked by shellcheck when it is
available and by `bash -n` when it is not.

The tests that matter most here are the redaction ones. A credential
that reaches a log line, an exception message or a traceback is a
credential that reaches a diagnostic bundle, and BUILD-BRIEF.md is
explicit that redaction happens at capture rather than on the way out.
"""

from __future__ import annotations

import html
import io
import json
import logging
import os
import shutil
import stat
import subprocess
import traceback
from pathlib import Path

import pytest

from catalyst.setup import credentials as creds
from catalyst.setup.first_run import FIELDS, SetupApp

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install" / "install.sh"
UPGRADE_SH = REPO_ROOT / "install" / "upgrade.sh"
UNIT_TEMPLATE = REPO_ROOT / "install" / "catalyst.service"

# Planted values. If any of these ever appears in a message, a log line
# or a rendered page, the test that noticed it has found a real leak.
FAKE_ALPACA_KEY = "PKFAKE123456789TEST"
FAKE_ALPACA_SECRET = "fakealpacasecret0000000000000000TESTONLY"
FAKE_ANTHROPIC_KEY = "sk-ant-fake0000000000000000000000TESTONLY"
FAKE_TOKEN = "fake-dashboard-access-code-0000"
ALL_FAKES = (FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET, FAKE_ANTHROPIC_KEY, FAKE_TOKEN)


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path, monkeypatch):
    """Point the store at a scratch file, and at a service user that does
    not exist so the tests never chown anything on the real machine."""
    monkeypatch.setenv("CATALYST_CREDENTIALS", str(tmp_path / "etc" / "credentials.json"))
    monkeypatch.setenv("CATALYST_SERVICE_USER", "catalyst-does-not-exist-in-tests")
    return tmp_path / "etc" / "credentials.json"


@pytest.fixture
def cred_file(_isolated_credentials):
    return _isolated_credentials


class FakeTransport:
    """An injected (url, headers) -> (status, body) transport.

    Records what it was asked for, so a test can assert the paper URL and
    the right headers were used without ever opening a socket.
    """

    def __init__(self, status=200, body="{}", raises: Exception | None = None):
        self.status = status
        self.body = body
        self.raises = raises
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers, timeout=15.0):
        self.calls.append((url, dict(headers)))
        if self.raises is not None:
            raise self.raises
        return self.status, self.body


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ==========================================================================
# The credential store
# ==========================================================================


def test_save_then_load_round_trip(cred_file):
    creds.save_credentials(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET,
                           FAKE_ANTHROPIC_KEY, FAKE_TOKEN)
    loaded = creds.load_credentials()
    assert loaded.alpaca_key == FAKE_ALPACA_KEY
    assert loaded.alpaca_secret == FAKE_ALPACA_SECRET
    assert loaded.anthropic_key == FAKE_ANTHROPIC_KEY
    assert loaded.dashboard_token == FAKE_TOKEN
    assert loaded.saved_at, "saved_at should record when setup happened"


def test_credentials_file_is_only_readable_by_its_owner(cred_file):
    creds.save_credentials(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET,
                           FAKE_ANTHROPIC_KEY, FAKE_TOKEN)
    assert _mode(cred_file) == 0o600, (
        f"credentials file is mode {oct(_mode(cred_file))}; anything but 0600 "
        "means another account on the machine can read the keys"
    )
    assert _mode(cred_file.parent) == 0o700, (
        f"credentials directory is mode {oct(_mode(cred_file.parent))}; it must "
        "be 0700 so the file cannot even be listed by others"
    )


def test_save_is_atomic_and_leaves_no_temporary_file_behind(cred_file):
    creds.save_credentials(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET,
                           FAKE_ANTHROPIC_KEY, FAKE_TOKEN)
    leftovers = [p.name for p in cred_file.parent.iterdir() if p.name != cred_file.name]
    assert leftovers == [], f"temporary files left in the credentials directory: {leftovers}"


def test_credentials_exist_is_false_before_setup(cred_file):
    assert creds.credentials_exist() is False


def test_credentials_exist_is_false_when_only_the_access_code_is_there(cred_file):
    """install.sh writes the access code before the owner has entered
    anything. That must not read as "setup complete", or the service
    would start trading with no broker keys."""
    creds.ensure_dashboard_token()
    assert cred_file.exists()
    assert creds.credentials_exist() is False


def test_credentials_exist_is_false_when_a_key_is_blank(cred_file):
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text(json.dumps({
        "alpaca_key": FAKE_ALPACA_KEY,
        "alpaca_secret": "",
        "anthropic_key": FAKE_ANTHROPIC_KEY,
        "dashboard_token": FAKE_TOKEN,
    }))
    assert creds.credentials_exist() is False


def test_credentials_exist_is_false_and_does_not_raise_on_a_corrupt_file(cred_file):
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text("{ this is not json")
    assert creds.credentials_exist() is False


def test_credentials_exist_is_true_after_a_complete_save(cred_file):
    creds.save_credentials(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET,
                           FAKE_ANTHROPIC_KEY, FAKE_TOKEN)
    assert creds.credentials_exist() is True


def test_saving_again_keeps_the_access_code_and_the_settings(cred_file):
    """Re-running setup to fix one key must not silently reset the
    budget or invalidate the link the owner bookmarked."""
    creds.save_credentials(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET, FAKE_ANTHROPIC_KEY,
                           FAKE_TOKEN, settings={"monthly_budget_usd": 7})
    creds.save_credentials("PKSECOND999999999", "second-secret-value-0000000000000000",
                           "sk-ant-second00000000000000000000", None)
    loaded = creds.load_credentials()
    assert loaded.dashboard_token == FAKE_TOKEN
    assert loaded.settings["monthly_budget_usd"] == 7
    assert loaded.alpaca_key == "PKSECOND999999999"


def test_ensure_dashboard_token_is_idempotent(cred_file):
    first = creds.ensure_dashboard_token()
    second = creds.ensure_dashboard_token()
    assert first == second and len(first) >= 24
    assert _mode(cred_file) == 0o600


def test_save_refuses_blank_values_with_a_readable_message(cred_file):
    with pytest.raises(creds.CredentialError) as exc:
        creds.save_credentials("", FAKE_ALPACA_SECRET, FAKE_ANTHROPIC_KEY, FAKE_TOKEN)
    assert "Alpaca API key ID" in str(exc.value)
    assert not cred_file.exists(), "a rejected save must not create the file"


def test_load_before_setup_explains_what_to_do(cred_file):
    with pytest.raises(creds.CredentialError) as exc:
        creds.load_credentials()
    assert "setup" in str(exc.value).lower()


# ==========================================================================
# Redaction - at capture, not on the way out
# ==========================================================================


def test_exception_from_deeper_code_cannot_carry_a_secret_out(cred_file, monkeypatch):
    """The redaction contract, exercised on the path that would break it:
    something several frames down raises with the key in its message."""

    def exploding_write(path, text):
        raise OSError(f"disk on fire while writing {FAKE_ALPACA_KEY} and {FAKE_ALPACA_SECRET}")

    monkeypatch.setattr(creds, "_write_private_file", exploding_write)

    with pytest.raises(creds.CredentialError) as exc_info:
        creds.save_credentials(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET,
                               FAKE_ANTHROPIC_KEY, FAKE_TOKEN)

    message = str(exc_info.value)
    assert "disk on fire" in message, "the real cause must survive redaction"
    for secret in (FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET):
        assert secret not in message, f"{secret!r} leaked into the exception message"
    assert creds.REDACTED in message


def test_the_whole_traceback_is_free_of_secrets(cred_file, monkeypatch):
    """`raise ... from e` would have carried the original, unredacted
    message out inside the chained traceback. This is the test that
    catches someone helpfully "fixing" the exception chaining."""

    def exploding_write(path, text):
        raise OSError(f"boom {FAKE_ANTHROPIC_KEY}")

    monkeypatch.setattr(creds, "_write_private_file", exploding_write)

    try:
        creds.save_credentials(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET,
                               FAKE_ANTHROPIC_KEY, FAKE_TOKEN)
    except creds.CredentialError as exc:
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    else:
        pytest.fail("expected a CredentialError")

    for secret in ALL_FAKES:
        assert secret not in rendered, f"{secret!r} leaked into the traceback"
    assert "The above exception was the direct cause" not in rendered


def test_a_corrupt_file_error_never_echoes_the_file_contents(cred_file):
    """The corrupt "file" is the secrets. Reporting it by quoting it back
    is the obvious and wrong way to write that error message."""
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text('{"alpaca_key": "' + FAKE_ALPACA_KEY + '" oops')

    with pytest.raises(creds.CredentialError) as exc_info:
        creds.load_credentials()
    assert FAKE_ALPACA_KEY not in str(exc_info.value)


def test_key_shaped_values_are_redacted_even_if_never_registered():
    """Belt and braces: a key pasted into the wrong box was never handed
    to remember_secret(), and must still not survive a message."""
    never_seen_alpaca = "PKNEVERREGISTERED999"
    never_seen_anthropic = "sk-ant-neverregistered0000000000"
    out = creds.redact(f"failed with {never_seen_alpaca} and {never_seen_anthropic}")
    assert never_seen_alpaca not in out
    assert never_seen_anthropic not in out
    assert out.count(creds.REDACTED) == 2


def test_redacting_filter_scrubs_log_records(cred_file):
    creds.remember_secret(FAKE_ALPACA_SECRET)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("catalyst.test.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    creds.install_redacting_filter(logger)

    logger.info("connecting with %s and %s", FAKE_ALPACA_SECRET, FAKE_ALPACA_KEY)
    logger.info("literal %s in the message itself", FAKE_ANTHROPIC_KEY)
    handler.flush()
    output = stream.getvalue()

    for secret in (FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET, FAKE_ANTHROPIC_KEY):
        assert secret not in output, f"{secret!r} reached a log line"
    assert creds.REDACTED in output


def test_credentials_repr_shows_nothing(cred_file):
    creds.save_credentials(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET,
                           FAKE_ANTHROPIC_KEY, FAKE_TOKEN)
    loaded = creds.load_credentials()
    for text in (repr(loaded), str(loaded), f"{loaded}"):
        for secret in ALL_FAKES:
            assert secret not in text
    assert loaded.status() == {
        "alpaca_key": True, "alpaca_secret": True,
        "anthropic_key": True, "dashboard_token": True,
        "anthropic_admin_key": False,   # optional, unset in this fixture
    }


# ==========================================================================
# Connection tests, against an injected transport
# ==========================================================================


ALPACA_OK_BODY = json.dumps({
    "id": "abc", "account_number": "PA123", "status": "ACTIVE",
    "buying_power": "1000.00", "equity": "1000.00", "multiplier": "1",
})


def test_alpaca_success_reports_the_account_and_uses_the_paper_url():
    transport = FakeTransport(200, ALPACA_OK_BODY)
    ok, message = creds.test_alpaca(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET, transport=transport)

    assert ok is True
    assert "ACTIVE" in message and "1000.00" in message
    url, headers = transport.calls[0]
    assert url == "https://paper-api.alpaca.markets/v2/account", (
        "the connection test must hit the PAPER account - paper only until "
        "performance is proven (BUILD-BRIEF.md)"
    )
    assert headers["APCA-API-KEY-ID"] == FAKE_ALPACA_KEY
    assert headers["APCA-API-SECRET-KEY"] == FAKE_ALPACA_SECRET
    assert len(transport.calls) == 1


def test_alpaca_rejection_quotes_the_real_error_from_alpaca():
    transport = FakeTransport(403, json.dumps({"message": "forbidden."}))
    ok, message = creds.test_alpaca(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET, transport=transport)

    assert ok is False
    assert "forbidden." in message, (
        "the owner must see what Alpaca actually said, not a paraphrase"
    )
    assert "403" in message
    assert "Paper Trading" in message, "a failure must say what to do about it"
    assert FAKE_ALPACA_SECRET not in message


def test_alpaca_non_json_error_body_is_printed_verbatim():
    """CLAUDE.md house rule 3: every zero gets its raw upstream response
    printed beside it. An HTML error page from a proxy is exactly the
    case where a summary tells you nothing."""
    transport = FakeTransport(502, "<html><body>Bad Gateway</body></html>")
    ok, message = creds.test_alpaca(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET, transport=transport)
    assert ok is False
    assert "Bad Gateway" in message
    assert "502" in message


def test_alpaca_network_failure_says_it_is_the_connection_not_the_keys():
    transport = FakeTransport(raises=OSError(f"getaddrinfo failed for {FAKE_ALPACA_KEY}"))
    ok, message = creds.test_alpaca(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET, transport=transport)
    assert ok is False
    assert "getaddrinfo failed" in message
    assert "internet connection" in message
    assert FAKE_ALPACA_KEY not in message, "even a network error must not echo the key"


def test_alpaca_empty_fields_do_not_call_out_at_all():
    transport = FakeTransport()
    ok, message = creds.test_alpaca("", "", transport=transport)
    assert ok is False
    assert transport.calls == []
    assert "Enter both" in message


def test_alpaca_test_does_not_read_the_removed_pdt_fields():
    """TRAPS.md: pattern_day_trader, daytrade_count, last_daytrade_count,
    daytrading_buying_power and last_daytrading_buying_power were removed
    from the Alpaca API in July 2026. Code referencing them breaks."""
    import inspect

    source = inspect.getsource(creds.test_alpaca.__wrapped__)
    code = "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("#"))
    for removed in ("pattern_day_trader", "daytrade_count", "daytrading_buying_power"):
        assert removed not in code, f"{removed} was removed from the Alpaca API"


ANTHROPIC_OK_BODY = json.dumps({
    "data": [{"id": "claude-sonnet-4-5", "type": "model"}], "has_more": True,
})


def test_anthropic_success_makes_exactly_one_request_to_the_models_endpoint():
    transport = FakeTransport(200, ANTHROPIC_OK_BODY)
    ok, message = creds.test_anthropic(FAKE_ANTHROPIC_KEY, transport=transport)

    assert ok is True
    assert "claude-sonnet-4-5" in message
    assert len(transport.calls) == 1, "checking a key must not cost more than one request"
    url, headers = transport.calls[0]
    assert url == "https://api.anthropic.com/v1/models?limit=1"
    assert headers["x-api-key"] == FAKE_ANTHROPIC_KEY
    assert headers["anthropic-version"] == creds.ANTHROPIC_VERSION


def test_anthropic_rejection_quotes_the_real_error():
    body = json.dumps({"type": "error",
                       "error": {"type": "authentication_error",
                                 "message": "invalid x-api-key"}})
    transport = FakeTransport(401, body)
    ok, message = creds.test_anthropic(FAKE_ANTHROPIC_KEY, transport=transport)
    assert ok is False
    assert "invalid x-api-key" in message
    assert "sk-ant-" in message, "the message should say what a real key looks like"
    assert FAKE_ANTHROPIC_KEY not in message


def test_anthropic_rate_limit_tells_the_owner_to_wait():
    transport = FakeTransport(429, json.dumps({"error": {"message": "rate_limit"}}))
    ok, message = creds.test_anthropic(FAKE_ANTHROPIC_KEY, transport=transport)
    assert ok is False
    assert "Wait a minute" in message


def test_anthropic_network_failure_is_reported_as_such():
    transport = FakeTransport(raises=TimeoutError("timed out"))
    ok, message = creds.test_anthropic(FAKE_ANTHROPIC_KEY, transport=transport)
    assert ok is False
    assert "timed out" in message
    assert "internet connection" in message


# ==========================================================================
# The first-run form
# ==========================================================================


def _app(**kwargs) -> SetupApp:
    defaults = dict(
        alpaca_tester=lambda k, s: (True, "Connected to your Alpaca paper account."),
        anthropic_tester=lambda k: (True, "Connected to Anthropic."),
    )
    defaults.update(kwargs)
    return SetupApp(**defaults)


def test_the_form_explains_every_field_in_plain_english():
    page = _app().handle("GET", "/").text
    for field in FIELDS:
        assert field.explanation in page, (
            f"the {field.name} field is rendered without its plain-English "
            "explanation - the owner is not a developer and cannot guess"
        )
        assert field.label in page


def test_the_alpaca_explanation_says_where_to_find_the_key():
    explanation = next(f for f in FIELDS if f.name == "alpaca_key").explanation
    assert "Paper Trading" in explanation and "API Keys" in explanation


def test_the_form_never_mentions_a_file_a_command_or_a_config_setting():
    """"Nobody is ever told to edit a config file" is a requirement, and
    it is broken the moment a path appears on the screen."""
    page = _app().handle("GET", "/").text
    for jargon in ("/etc/", "/var/lib", ".env", "systemctl", "sudo ", "chmod",
                   "credentials.json", "environment variable", "config file"):
        assert jargon not in page, f"the setup page shows {jargon!r} to a non-technical owner"


def test_the_form_offers_a_test_button_for_each_credential():
    page = _app().handle("GET", "/").text
    assert page.count("Test this connection") == 2
    assert "testAlpaca()" in page and "testAnthropic()" in page


def test_test_buttons_return_the_real_error_from_upstream():
    app = _app(alpaca_tester=lambda k, s: (False, 'Alpaca said: "forbidden."'))
    response = app.handle("POST", "/test/alpaca",
                          json.dumps({"alpaca_key": FAKE_ALPACA_KEY,
                                      "alpaca_secret": FAKE_ALPACA_SECRET}).encode(),
                          {"content-type": "application/json"})
    payload = response.json()
    assert payload["ok"] is False
    assert "forbidden." in payload["message"]


def test_test_button_passes_what_was_typed_to_the_tester():
    seen = {}

    def tester(key, secret):
        seen["key"], seen["secret"] = key, secret
        return True, "fine"

    app = _app(alpaca_tester=tester)
    app.handle("POST", "/test/alpaca",
               json.dumps({"alpaca_key": FAKE_ALPACA_KEY,
                           "alpaca_secret": FAKE_ALPACA_SECRET}).encode(),
               {"content-type": "application/json"})
    assert seen == {"key": FAKE_ALPACA_KEY, "secret": FAKE_ALPACA_SECRET}


def _save_body(**overrides) -> bytes:
    body = {
        "alpaca_key": FAKE_ALPACA_KEY,
        "alpaca_secret": FAKE_ALPACA_SECRET,
        "anthropic_key": FAKE_ANTHROPIC_KEY,
        "monthly_budget_usd": "5",
    }
    body.update(overrides)
    return json.dumps(body).encode()


def test_saving_writes_the_file_and_signals_the_service(cred_file):
    signalled = []
    app = _app(on_saved=lambda: signalled.append(True))
    response = app.handle("POST", "/save", _save_body(), {"content-type": "application/json"})

    payload = response.json()
    assert payload["ok"] is True, payload["message"]
    assert signalled == [True], "the service is never told setup finished"
    assert creds.credentials_exist() is True
    assert _mode(cred_file) == 0o600
    loaded = creds.load_credentials()
    assert loaded.alpaca_key == FAKE_ALPACA_KEY
    assert loaded.settings["monthly_budget_usd"] == 5.0


def test_saving_refuses_when_the_broker_connection_fails_and_writes_nothing(cred_file):
    app = _app(alpaca_tester=lambda k, s: (False, 'Alpaca said: "forbidden."'))
    payload = app.handle("POST", "/save", _save_body(),
                         {"content-type": "application/json"}).json()

    assert payload["ok"] is False
    assert "forbidden." in payload["message"]
    assert "Nothing was saved" in payload["message"]
    assert not cred_file.exists()


def test_saving_refuses_when_the_research_key_fails_and_writes_nothing(cred_file):
    app = _app(anthropic_tester=lambda k: (False, "Anthropic said: \"invalid x-api-key\""))
    payload = app.handle("POST", "/save", _save_body(),
                         {"content-type": "application/json"}).json()
    assert payload["ok"] is False
    assert "invalid x-api-key" in payload["message"]
    assert not cred_file.exists()


def test_saving_refuses_blank_boxes_by_name(cred_file):
    payload = _app().handle("POST", "/save", _save_body(anthropic_key="  "),
                            {"content-type": "application/json"}).json()
    assert payload["ok"] is False
    assert "Anthropic key" in payload["message"]
    assert not cred_file.exists()


def test_saving_rejects_a_nonsense_budget(cred_file):
    payload = _app().handle("POST", "/save", _save_body(monthly_budget_usd="lots"),
                            {"content-type": "application/json"}).json()
    assert payload["ok"] is False
    assert "Try 5." in payload["message"]
    assert not cred_file.exists()


def test_once_saved_the_page_never_shows_the_values_again(cred_file):
    app = _app()
    app.handle("POST", "/save", _save_body(), {"content-type": "application/json"})
    token = creds.load_credentials().dashboard_token

    page = app.handle("GET", f"/?code={token}").text
    assert "Catalyst is set up" in page
    for secret in (FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET, FAKE_ANTHROPIC_KEY):
        assert secret not in page, "a saved credential was rendered back to the browser"


def test_the_replacement_form_is_blank_not_prefilled(cred_file):
    app = _app()
    app.handle("POST", "/save", _save_body(), {"content-type": "application/json"})
    token = creds.load_credentials().dashboard_token

    page = app.handle("GET", f"/?code={token}&replace=1").text
    assert "Welcome" in page
    for secret in (FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET, FAKE_ANTHROPIC_KEY):
        assert secret not in page
    assert 'value=""' not in page.replace('value="5"', "")  # no prefilled secret boxes


def test_no_response_anywhere_in_the_flow_contains_a_secret(cred_file):
    app = _app()
    responses = [
        app.handle("GET", "/"),
        app.handle("POST", "/test/alpaca",
                   json.dumps({"alpaca_key": FAKE_ALPACA_KEY,
                               "alpaca_secret": FAKE_ALPACA_SECRET}).encode(),
                   {"content-type": "application/json"}),
        app.handle("POST", "/save", _save_body(), {"content-type": "application/json"}),
    ]
    token = creds.load_credentials().dashboard_token
    responses.append(app.handle("GET", f"/?code={token}"))
    responses.append(app.handle("GET", "/health"))
    for response in responses:
        for secret in (FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET, FAKE_ANTHROPIC_KEY):
            assert secret not in response.text, f"{secret!r} came back out of the setup UI"


def test_health_answers_without_the_access_code(cred_file):
    """install.sh polls this to prove the service came up, before the
    owner has typed anything anywhere."""
    token = creds.ensure_dashboard_token()
    payload = _app().handle("GET", "/health").json()
    assert payload == {"status": "awaiting_setup", "setup_required": True}

    saved = _app().handle("POST", f"/save?code={token}", _save_body(),
                          {"content-type": "application/json"})
    assert saved.json()["ok"] is True, saved.text
    payload = _app().handle("GET", "/health").json()
    assert payload == {"status": "configured", "setup_required": False}


def test_the_page_is_locked_without_the_access_code(cred_file):
    token = creds.ensure_dashboard_token()
    response = _app().handle("GET", "/")
    assert response.status == 403
    assert "access code" in response.text
    assert token not in response.text, "the locked page must not print the code it wants"


def test_the_access_code_lets_you_in_and_is_remembered(cred_file):
    token = creds.ensure_dashboard_token()
    response = _app().handle("GET", f"/?code={token}")
    assert response.status == 200
    cookie = dict(response.headers).get("Set-Cookie", "")
    assert cookie.startswith("catalyst_access=")
    assert "HttpOnly" in cookie

    with_cookie = _app().handle("GET", "/", headers={"cookie": f"catalyst_access={token}"})
    assert with_cookie.status == 200


def test_a_wrong_access_code_is_refused(cred_file):
    creds.ensure_dashboard_token()
    assert _app().handle("GET", "/?code=not-the-right-code").status == 403


def test_an_unknown_address_says_so_in_plain_english(cred_file):
    response = _app().handle("GET", "/nowhere")
    assert response.status == 404
    assert "nothing at this address" in response.text.lower()


def test_the_app_can_be_mounted_under_a_prefix(cred_file):
    """The stage-6 dashboard mounts this at its own marked mount point."""
    app = _app(path_prefix="/setup")
    assert app.handle("GET", "/setup/").status == 200
    assert app.handle("GET", "/setup/health").json()["setup_required"] is True


# ==========================================================================
# The shell scripts
# ==========================================================================


def _shell_check(script: Path) -> tuple[str, subprocess.CompletedProcess]:
    if shutil.which("shellcheck"):
        return "shellcheck", subprocess.run(
            ["shellcheck", "--severity=warning", str(script)],
            capture_output=True, text=True, timeout=60)
    return "bash -n", subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("script", [INSTALL_SH, UPGRADE_SH], ids=lambda p: p.name)
def test_shell_scripts_pass_static_checking(script):
    tool, result = _shell_check(script)
    assert result.returncode == 0, (
        f"{tool} rejected {script.name}:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize("script", [INSTALL_SH, UPGRADE_SH], ids=lambda p: p.name)
def test_shell_scripts_stop_on_the_first_error(script):
    assert "set -euo pipefail" in script.read_text()


def _logical_lines(text: str) -> list[str]:
    lines, buffer = [], ""
    for raw in text.splitlines():
        buffer += raw.rstrip("\\").rstrip() if raw.rstrip().endswith("\\") else raw
        if raw.rstrip().endswith("\\"):
            buffer += " "
            continue
        lines.append(buffer)
        buffer = ""
    if buffer:
        lines.append(buffer)
    return lines


@pytest.mark.parametrize("script", [INSTALL_SH, UPGRADE_SH], ids=lambda p: p.name)
def test_every_failure_says_what_failed_and_what_to_do(script):
    """"Setup failed" is not acceptable output. Every fail() call has to
    carry three things: what went wrong, the exact error, and at least
    one instruction the owner can follow."""
    import shlex

    calls = [line.strip() for line in _logical_lines(script.read_text())
             if line.strip().startswith("fail ")]
    assert len(calls) >= 8, f"only {len(calls)} guarded failure paths in {script.name}"

    for call in calls:
        try:
            tokens = shlex.split(call)
        except ValueError:
            # Nested command substitution the tokenizer cannot follow;
            # fall back to counting the top-level quoted arguments.
            tokens = ["fail"] + [t for t in call.split('" "')]
        assert len(tokens) >= 4, (
            f"a failure path in {script.name} gives fewer than three things "
            f"(what failed / exact error / what to do):\n  {call[:160]}"
        )
        instruction = tokens[3]
        assert len(instruction) > 20, (
            f"the 'what to do' for a failure in {script.name} is too short to "
            f"act on: {instruction!r}"
        )


@pytest.mark.parametrize("script", [INSTALL_SH, UPGRADE_SH], ids=lambda p: p.name)
def test_failure_output_has_all_three_sections(script):
    text = script.read_text()
    for heading in ("WHAT WENT WRONG", "THE EXACT ERROR", "WHAT TO DO ABOUT IT"):
        assert heading in text
    # Only what the script can actually print - comments explaining the
    # rule are allowed to quote the thing the rule forbids.
    printable = "\n".join(line for line in text.splitlines()
                          if not line.lstrip().startswith("#"))
    for banned in ("Setup failed", "Installation failed", "Something went wrong."):
        assert banned not in printable, (
            f"{banned!r} can be printed on its own, with no explanation"
        )


def test_install_reports_a_real_failure_end_to_end(tmp_path):
    """Run the installer for real against a deliberately broken layout
    and read what it prints. A failure message is only as good as what
    actually comes out of it."""
    fake_repo = tmp_path / "broken-copy"
    (fake_repo / "install").mkdir(parents=True)
    shutil.copy(INSTALL_SH, fake_repo / "install" / "install.sh")
    shutil.copy(UNIT_TEMPLATE, fake_repo / "install" / "catalyst.service")
    # pyproject.toml deliberately absent: the program files are missing.

    result = subprocess.run(
        ["bash", str(fake_repo / "install" / "install.sh"), "--no-service"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "CATALYST_INSTALL_LOG": str(tmp_path / "install.log")},
    )
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert "WHAT WENT WRONG" in output
    assert "THE EXACT ERROR" in output
    assert "WHAT TO DO ABOUT IT" in output
    assert "     1. " in output, "the instructions must be numbered steps"
    assert "safe to run again" in output, "the owner must be told nothing was broken"
    # Whatever it stopped on, it named the step and gave the log.
    assert "INSTALL STOPPED - step" in output
    assert "install.log" in output


def test_install_is_idempotent_by_construction():
    """Re-running must not clobber the database or the saved keys. Check
    the code that could: there is no unguarded delete of either."""
    text = INSTALL_SH.read_text()
    for destructive in ("rm -f ${CATALYST_DB}", "rm -rf ${CATALYST_STATE_DIR}",
                        "rm -f ${CATALYST_CREDENTIALS}", "rm -rf ${CATALYST_ETC}",
                        "> ${CATALYST_DB}"):
        assert destructive not in text, f"install.sh can destroy state: {destructive}"
    # Check-then-act on everything that already exists.
    assert 'id -u "${CATALYST_SERVICE_USER}"' in text
    assert 'getent group "${CATALYST_SERVICE_USER}"' in text
    assert "venv_is_good" in text
    assert "cmp -s" in text, "the unit file is rewritten without checking it changed"
    assert "DB_EXISTED" in text


def test_install_only_ever_adds_to_an_existing_database():
    """init_db is CREATE TABLE IF NOT EXISTS throughout, which is what
    makes a second run safe. install.sh must call that, not a fresh
    create, and must never drop anything."""
    text = INSTALL_SH.read_text()
    assert "from catalyst.storage import init_db; init_db(" in text
    assert "DROP TABLE" not in text.upper()


# ==========================================================================
# The systemd unit
# ==========================================================================


def test_unit_template_has_everything_the_service_needs():
    unit = UNIT_TEMPLATE.read_text()
    assert "After=network-online.target" in unit
    assert "Wants=network-online.target" in unit
    assert "Restart=on-failure" in unit
    assert "User=__USER__" in unit
    assert "Environment=CATALYST_DB=__DB__" in unit
    assert "Environment=CATALYST_CREDENTIALS=__CREDENTIALS__" in unit
    assert "ExecStart=__VENV_PYTHON__ -m catalyst.orchestrator.scheduler" in unit
    assert "WantedBy=multi-user.target" in unit


def test_unit_carries_no_secrets_in_its_environment():
    """`systemctl show catalyst` prints Environment= lines to anyone who
    can run it. Credentials go in the 0600 file, never here."""
    unit = UNIT_TEMPLATE.read_text()
    for banned in ("APCA", "ANTHROPIC_API_KEY", "ALPACA_SECRET", "DASHBOARD_TOKEN"):
        assert banned not in unit


def test_installer_fills_in_every_placeholder_in_the_unit():
    import re

    placeholders = set(re.findall(r"__[A-Z_]+__", UNIT_TEMPLATE.read_text()))
    assert placeholders, "the unit template has no placeholders at all"
    install_text = INSTALL_SH.read_text()
    for placeholder in placeholders:
        assert f"s|{placeholder}|" in install_text, (
            f"install.sh never substitutes {placeholder}, so the installed "
            "service file would be broken"
        )
    assert "grep -q '__[A-Z_]*__'" in install_text, (
        "install.sh should check its own rendering left no blanks"
    )


def test_service_binds_the_dashboard_where_the_brief_says():
    unit = UNIT_TEMPLATE.read_text()
    assert "Environment=CATALYST_BIND=0.0.0.0" in unit
    assert "Environment=CATALYST_PORT=__PORT__" in unit


# ==========================================================================
# The upgrade path
# ==========================================================================


def test_upgrade_backs_up_before_it_changes_anything():
    text = UPGRADE_SH.read_text()
    backup_at = text.index("Backing up the database and your saved keys")
    install_at = text.index("Installing the new version")
    fetch_at = text.index("Fetching the new version")
    assert backup_at < fetch_at < install_at, (
        "the backup must happen before the new version is fetched or installed"
    )


def test_upgrade_backs_up_both_the_database_and_the_credentials():
    text = UPGRADE_SH.read_text()
    assert "src.backup(dst)" in text, "a plain file copy of a live database can be corrupt"
    assert "PRAGMA integrity_check" in text, "an unverified backup is not a backup"
    assert '"${BACKUP_PATH}/credentials.json"' in text
    assert "STAMP=" in text and "date +%Y%m%d-%H%M%S" in text


def test_upgrade_runs_the_whole_test_suite_and_rolls_back_when_it_fails():
    text = UPGRADE_SH.read_text()
    assert "-m pytest" in text
    assert 'if [ "${TEST_RC}" -ne 0 ]; then' in text
    assert text.index('rollback "The new version failed its own tests') > text.index("TEST_RC=0")


def test_upgrade_does_not_capture_the_test_result_with_a_dollar_question_mark():
    """A bug this suite was written for, after it happened: `set +e` does
    not disable the ERR trap, so `TEST_RC=$?` after a failing pytest run
    sent the script to its unexpected-error handler instead of to
    rollback() - the one path the whole script exists for. Inside an `if`
    condition the ERR trap does not fire."""
    text = UPGRADE_SH.read_text()
    assert "TEST_RC=$?" not in text
    assert 'if ! (cd "${REPO_DIR}" && "${VENV_PY}" -m pytest)' in text


def test_upgrade_rollback_restores_code_database_and_credentials_then_restarts():
    text = UPGRADE_SH.read_text()
    rollback = text[text.index("rollback() {"):text.index("printf 'Catalyst upgrade")]
    assert "git -C \"${REPO_DIR}\" reset --hard \"${OLD_COMMIT}\"" in rollback
    assert "pip install --quiet \"${REPO_DIR}[dev]\"" in rollback
    assert "${BACKUP_PATH}/catalyst.db" in rollback
    assert "${BACKUP_PATH}/credentials.json" in rollback
    assert "service_do start" in rollback
    assert "rollback_failed" in rollback, (
        "the rollback must check its own work and say so if it could not finish"
    )


def test_upgrade_rolls_back_if_the_new_version_will_not_start():
    text = UPGRADE_SH.read_text()
    assert 'rollback "The new version passed its tests but would not start."' in text
    assert 'rollback "The new version started and then stopped again straight away."' in text


def test_upgrade_restores_credentials_with_their_permissions_intact():
    text = UPGRADE_SH.read_text()
    assert 'run chmod 0600 "${BACKUP_PATH}/credentials.json"' in text
    assert 'run chmod 0600 "${CATALYST_CREDENTIALS}"' in text


def test_upgrade_refuses_to_throw_away_hand_edits():
    text = UPGRADE_SH.read_text()
    assert "status --porcelain --untracked-files=no" in text, (
        "installing the package leaves build artifacts in the folder; counting "
        "those as hand-edits would block every upgrade after the first install"
    )


def test_the_build_artifacts_pip_leaves_behind_are_ignored():
    """install.sh runs `pip install <repo>`, which writes a build/ folder
    into the repository. Found by running the installer for real: the
    next upgrade then refused to start, because the folder looked
    modified."""
    ignored = (REPO_ROOT / ".gitignore").read_text().split()
    assert "build/" in ignored


# --------------------------------------------------------------------------
# The upgrade, actually executed. Offline: git works against a local bare
# repository, and pip/pytest are a stub whose exit status the test picks.
# Static checks said the rollback code was there; only running it showed
# that it was never reached.
# --------------------------------------------------------------------------


def _fake_installation(tmp_path, pytest_exit_code: int) -> dict:
    import textwrap

    src = tmp_path / "src"
    (src / "install").mkdir(parents=True)
    shutil.copy(UPGRADE_SH, src / "install" / "upgrade.sh")
    (src / "version.txt").write_text("old\n")

    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]

    def run_git(*args, cwd):
        return subprocess.run([*git, *args], cwd=cwd, capture_output=True,
                              text=True, check=True, timeout=30)

    run_git("init", "-q", cwd=src)
    run_git("add", "-A", cwd=src)
    run_git("commit", "-qm", "old version", cwd=src)

    origin = tmp_path / "origin.git"
    subprocess.run([*git, "clone", "-q", "--bare", str(src), str(origin)],
                   capture_output=True, check=True, timeout=60)
    repo = tmp_path / "repo"
    subprocess.run([*git, "clone", "-q", str(origin), str(repo)],
                   capture_output=True, check=True, timeout=60)
    old_commit = subprocess.run([*git, "rev-parse", "HEAD"], cwd=repo,
                                capture_output=True, text=True, check=True).stdout.strip()

    # publish a "new version"
    (src / "version.txt").write_text("new\n")
    run_git("commit", "-qam", "new version", cwd=src)
    run_git("remote", "add", "origin", str(origin), cwd=src)
    run_git("push", "-q", "origin", "HEAD", cwd=src)
    new_commit = subprocess.run([*git, "rev-parse", "HEAD"], cwd=src,
                                capture_output=True, text=True, check=True).stdout.strip()

    # A stand-in for the virtual environment's python: pip and pytest are
    # answered locally (no network, no real test run), everything else -
    # including the sqlite backup - is the real interpreter.
    venv_bin = tmp_path / "opt" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_python = venv_bin / "python"
    fake_python.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        for arg in "$@"; do
          case "$arg" in
            pytest) echo "fake suite: exiting {pytest_exit_code}"; exit {pytest_exit_code} ;;
            pip)    exit 0 ;;
          esac
        done
        exec {os.sys.executable} "$@"
        """))
    fake_python.chmod(0o755)

    state = tmp_path / "var"
    state.mkdir()
    db = state / "catalyst.db"
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE survives_rollback (v TEXT)")
    conn.execute("INSERT INTO survives_rollback VALUES ('yes')")
    conn.commit()
    conn.close()

    etc = tmp_path / "etc"
    etc.mkdir()
    cred = etc / "credentials.json"
    cred.write_text(json.dumps({"alpaca_key": FAKE_ALPACA_KEY,
                                "alpaca_secret": FAKE_ALPACA_SECRET,
                                "anthropic_key": FAKE_ANTHROPIC_KEY,
                                "dashboard_token": FAKE_TOKEN}))
    cred.chmod(0o600)

    env = {
        **os.environ,
        "CATALYST_HOME": str(tmp_path / "opt"),
        "CATALYST_STATE_DIR": str(state),
        "CATALYST_DB": str(db),
        "CATALYST_ETC": str(etc),
        "CATALYST_CREDENTIALS": str(cred),
        "CATALYST_BACKUP_DIR": str(tmp_path / "backups"),
        "CATALYST_MANAGE_SERVICE": "0",
        "CATALYST_SERVICE_USER": os.environ.get("USER", "root"),
        "CATALYST_UPGRADE_LOG": str(tmp_path / "upgrade.log"),
        "PYTHONPATH": str(REPO_ROOT),
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
    }
    return {"repo": repo, "db": db, "cred": cred, "env": env,
            "old_commit": old_commit, "new_commit": new_commit,
            "backups": tmp_path / "backups"}


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required to upgrade")
def test_upgrade_rolls_back_for_real_when_the_new_version_fails_its_tests(tmp_path):
    setup = _fake_installation(tmp_path, pytest_exit_code=1)

    result = subprocess.run(
        ["bash", str(setup["repo"] / "install" / "upgrade.sh")],
        capture_output=True, text=True, timeout=120, env=setup["env"],
    )
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "PUTTING THE OLD VERSION BACK" in output
    assert "you are back on the version you were running before" in output
    assert "UPGRADE STOPPED unexpectedly" not in output, (
        "a failing test suite must reach rollback(), not the unexpected-error handler"
    )

    assert _head(setup["repo"]) == setup["old_commit"], "the code was not put back"
    assert (setup["repo"] / "version.txt").read_text().strip() == "old"

    import sqlite3

    rows = sqlite3.connect(setup["db"]).execute(
        "SELECT count(*) FROM survives_rollback").fetchone()[0]
    assert rows == 1, "the database did not survive the rollback"
    assert json.loads(setup["cred"].read_text())["alpaca_key"] == FAKE_ALPACA_KEY
    assert _mode(setup["cred"]) == 0o600, "the restored keys file lost its lock"

    backup = next(setup["backups"].iterdir())
    for expected in ("catalyst.db", "credentials.json", "README.txt", "test-output.txt"):
        assert (backup / expected).exists(), f"the backup is missing {expected}"
    for secret in ALL_FAKES:
        assert secret not in output, "a credential reached the upgrade output"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required to upgrade")
def test_upgrade_completes_when_the_new_version_passes_its_tests(tmp_path):
    setup = _fake_installation(tmp_path, pytest_exit_code=0)

    result = subprocess.run(
        ["bash", str(setup["repo"] / "install" / "upgrade.sh")],
        capture_output=True, text=True, timeout=120, env=setup["env"],
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "Upgrade complete." in output
    assert "PUTTING THE OLD VERSION BACK" not in output
    assert _head(setup["repo"]) == setup["new_commit"]
    assert (setup["repo"] / "version.txt").read_text().strip() == "new"
    assert next(setup["backups"].iterdir()).joinpath("catalyst.db").exists()


# ==========================================================================
# The service entry point the unit starts
# ==========================================================================


def test_scheduler_serves_setup_and_waits_rather_than_crashing(tmp_path, monkeypatch):
    """The unit runs this. On a fresh machine there are no credentials
    and no pipeline yet; it must stay up and serve the setup page rather
    than exit and be restarted forever."""
    from catalyst.orchestrator import scheduler

    monkeypatch.setenv("CATALYST_DB", str(tmp_path / "state" / "catalyst.db"))
    monkeypatch.setattr(scheduler, "start_setup_server", lambda: None)

    assert scheduler.main(["--once"]) == 0
    assert (tmp_path / "state" / "catalyst.db").exists()


def test_scheduler_selftest_is_what_the_installer_can_call(tmp_path, monkeypatch):
    from catalyst.orchestrator import scheduler

    monkeypatch.setenv("CATALYST_DB", str(tmp_path / "state" / "catalyst.db"))
    assert scheduler.main(["--selftest"]) == 0


def test_scheduler_survives_a_failing_cycle_and_says_so(tmp_path, monkeypatch, capsys):
    """The scheduler is wired to the real pipeline now (stage 5). The
    invariant is unchanged in spirit: a cycle that cannot run must be
    LOGGED as a failure while the service stays up - never silent, never
    crash-looping, never leaking a secret into the log."""
    from catalyst.orchestrator import scheduler

    monkeypatch.setenv("CATALYST_DB", str(tmp_path / "state" / "catalyst.db"))
    monkeypatch.setattr(scheduler, "start_setup_server", lambda: None)

    def boom(db_file):
        raise RuntimeError("wired pipeline exploded for the test")

    monkeypatch.setattr(scheduler, "_run_one_cycle", boom)
    creds.save_credentials(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET,
                           FAKE_ANTHROPIC_KEY, FAKE_TOKEN)

    assert scheduler.main(["--once"]) == 0
    logged = capsys.readouterr().out
    assert "A trading cycle failed" in logged
    assert "wired pipeline exploded" in logged   # the traceback, kept
    for secret in ALL_FAKES:
        assert secret not in logged


def test_scheduler_installs_the_redacting_filter_on_the_root_logger():
    from catalyst.orchestrator import scheduler

    scheduler.configure_logging()
    root = logging.getLogger()
    assert any(isinstance(f, creds.RedactingFilter) for f in root.filters), (
        "without this, any log line anywhere in the system could print a key"
    )


# --------------------------------------------------------------------------
# The setup page must be legible in BOTH colour schemes.
#
# Owner report 2026-08-10: "the initial setup is a bit hard to see with
# the colours". Reproduced in a dark-mode browser: the page declared
# `color-scheme: light dark` while every colour in the sheet was a
# hardcoded LIGHT value, so the field explanations, the Test buttons and
# the whole privacy note rendered near-invisible. This is the one screen
# whose failure the owner cannot route around - there is no dashboard to
# fall back to until it has been used.
# --------------------------------------------------------------------------


class TestSetupPageLegibility:
    def _tokens(self, block: str) -> set:
        import re
        return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))

    def test_dark_scheme_redefines_every_colour_token(self):
        """The exact bug class: declaring dark support and then leaving
        the light values in place. Every token defined for light must be
        given a dark value too."""
        from catalyst.setup.first_run import _STYLE

        light = _STYLE.split("@media")[0]
        dark = _STYLE.split("@media", 1)[1]
        missing = self._tokens(light) - self._tokens(dark) - {"--color-scheme"}
        assert not missing, (
            f"tokens with no dark value: {sorted(missing)} - the page would "
            "render these light-mode colours on a dark background")

    def test_no_colour_is_hardcoded_outside_the_token_blocks(self):
        """Rules must consume tokens, not literals - a literal cannot
        follow the theme, which is how the original bug happened."""
        import re
        from catalyst.setup.first_run import _STYLE

        rules = _STYLE.split("}", 2)[2]        # past both :root blocks
        rules = re.sub(r"@media[^{]*\{[^}]*\{[^}]*\}[^}]*\}", "", rules)
        literals = [h for h in re.findall(r"#[0-9a-fA-F]{3,8}\b", rules)
                    if h.lower() not in ("#fff", "#ffffff")]
        assert not literals, f"hardcoded colours outside the palette: {literals}"

    def test_body_declares_its_own_background_and_ink(self):
        """Without both, the browser's own dark background shows through
        under text coloured for light - which is what happened."""
        from catalyst.setup.first_run import _STYLE

        body = _STYLE.split("body {", 1)[1].split("}", 1)[0]
        assert "background: var(" in body and "color: var(" in body

    def test_the_two_account_choices_are_separate_hit_targets(self):
        """One of these spends real money. Run together as inline text
        they read as a single paragraph."""
        from catalyst.setup.first_run import render_setup_page

        page = render_setup_page()
        assert page.count('class="radio"') == 2
        assert "<b>Live account &mdash; REAL MONEY.</b>" in page
        assert "label.radio { display: flex" in page

    def test_every_field_explanation_still_renders(self):
        """The restyle must not have dropped the text that makes this
        page usable by someone who is not a developer."""
        from catalyst.setup.first_run import FIELDS, render_setup_page

        page = render_setup_page()
        for f in FIELDS:
            assert html.escape(f.explanation)[:60] in page, f.name


class TestBudgetFieldTellsTheTruth:
    """Owner report 2026-08-10: entered 20 in the setup page's budget
    box, then saw $5 everywhere and had no way to know why.

    The behaviour was correct - governor.authorize lets the owner figure
    only ever LOWER the cap (BUILD-BRIEF: base $5/month, hard; it rises
    only out of realised profit). The PAGE was the defect: it invited a
    number it would silently ignore."""

    def test_the_explanation_states_the_range_and_the_bots_own_limit(self):
        """CONTRACT CHANGED TWICE at the owner's request - the field sets
        the budget both ways and now has no ceiling. What the page must
        still make plain: that it obeys whatever is set, what the figure
        costs, that 0 stops it, and that the bot topping itself up out of
        profit is a SEPARATE thing bounded separately."""
        from catalyst.setup.first_run import FIELDS

        f = next(f for f in FIELDS if f.name == "monthly_budget_usd")
        text = (f.label + " " + f.explanation).lower()
        assert "no ceiling" in text
        assert "obeys you" in text
        assert "25" in f.explanation           # where the hurdle turns severe
        assert "$8" in f.explanation           # what the bot may add itself
        assert "0" in f.explanation            # stopping entirely

    def test_the_form_shows_the_annual_hurdle_as_the_number_is_typed(self):
        """The cost of the choice, at the moment of choosing: a fixed
        monthly bill on a $1,000 account is a return the strategy has to
        clear before a trade counts as good."""
        from catalyst.setup.first_run import render_setup_page

        page = render_setup_page()
        assert 'oninput="budgetHint()"' in page
        assert "monthly_budget_usd_hint" in page
        assert "% a year on a $1,000 account" in page
        assert "ADVICE_USD = 25" in page, (
            "$25 is now advice printed at the point of choosing, not a wall")
        assert 'max="25"' not in page, "the field must not cap the owner"

    def test_a_bigger_owner_figure_raises_the_cap_with_no_ceiling(
            self, tmp_path):
        """CONTRACT CHANGED TWICE at the owner's request: the figure sets
        the budget in both directions, and no longer has a ceiling of its
        own. The guard against a mistyped figure moved to the point of
        entry, where a confirmation can be asked for."""
        import sqlite3
        from decimal import Decimal

        from catalyst.cost import CostEstimate
        from catalyst.cost import governor as gov
        from catalyst.cost.governor import BASE_CAP_CENTS

        conn = sqlite3.connect(tmp_path / "g.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        est = CostEstimate(estimated_cents=BASE_CAP_CENTS + Decimal("50"),
                           basis="test", kind="scheduled", component="research")
        d = gov.authorize(est, conn, Decimal("0.10"),
                          owner_monthly_cap_cents=Decimal("2000"))  # $20
        assert d.cap_cents == Decimal("2000"), (
            "the owner's deliberate figure is the budget now")
        assert d.authorized is True
        # ... and there is no ceiling of its own on the way up. The
        # guard against a mistyped figure is at the point of entry, not
        # here - see TestTheBudgetFieldGuardsAgainstATypo.
        d2 = gov.authorize(est, conn, Decimal("0.10"),
                           owner_monthly_cap_cents=Decimal("999999"))
        assert d2.cap_cents == Decimal("999999")
        conn.close()

    def test_a_smaller_owner_figure_does_tighten_the_cap(self, tmp_path):
        import sqlite3
        from decimal import Decimal

        from catalyst.cost import CostEstimate
        from catalyst.cost import governor as gov

        conn = sqlite3.connect(tmp_path / "g.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        est = CostEstimate(estimated_cents=Decimal("150"), basis="test",
                           kind="scheduled", component="research")
        d = gov.authorize(est, conn, Decimal("0.10"),
                          owner_monthly_cap_cents=Decimal("100"))   # $1
        assert d.cap_cents == Decimal("100")
        assert d.authorized is False
        conn.close()


# ==========================================================================
# The upgrade has to say what actually changed.
#
# Owner-reported 2026-08-10: "i am running the upgrade but the dashboard
# changes dont seem to have taken effect ... The version now and version
# before have been 0.1.0 for a while now."
#
# The upgrade was working correctly. The changes were published to a
# branch the server does not follow, so `git pull --ff-only` had nothing
# to fetch - and the report still ended with "Upgrade complete", the
# version string being hand-maintained and identical either way. An
# evening was then spent on browser caches.
# ==========================================================================


class TestTheUpgradeReportsWhatChanged:
    def _text(self):
        return UPGRADE_SH.read_text()

    def _summary(self):
        """Everything from the success banner to the end. Split-and-take
        [1] truncates at the SECOND occurrence of the phrase and silently
        hid half the report from these assertions."""
        text = self._text()
        return text[text.index("Upgrade complete"):]

    def test_the_version_string_is_not_the_only_thing_reported(self):
        """__version__ is hand-maintained and can sit still across many
        real changes. The commit cannot."""
        summary = self._summary()
        assert "OLD_COMMIT" in summary and "NEW_COMMIT" in summary

    def test_the_report_names_the_branch_the_machine_follows(self):
        """'Which branch is this server on' is the question that ends the
        confusion, so the answer is printed rather than looked up."""
        assert "UPGRADE_BRANCH" in self._text()
        summary = self._summary()
        assert "UPGRADE_BRANCH" in summary

    def test_nothing_fetched_does_not_read_as_a_successful_change(self):
        summary = self._summary()
        assert "NOTHING_FETCHED" in summary
        assert "NOTHING CHANGED" in summary
        assert "not a failure" in summary

    def test_the_build_hash_is_printed_so_a_cached_page_is_provable(self):
        """The sidebar prints the same hash. Equal means you are looking
        at the new version; different means the browser cached it."""
        summary = self._summary()
        assert "NEW_BUILD_HASH" in summary
        assert "cached" in summary

    @pytest.mark.parametrize("name", ["NOTHING_FETCHED", "UPGRADE_BRANCH",
                                      "NEW_BUILD_HASH"])
    def test_every_reported_variable_has_a_default(self, name):
        """set -u is on and the fetch phase is skippable, so a variable
        read in the summary but only assigned inside that phase aborts
        the script at the very end - after the upgrade has happened."""
        text = self._text()
        init = text.split("BACKUP_MADE=0")[0]
        assert f"{name}=" in init, (
            f"{name} is read in the summary but never initialised")


def test_the_package_version_and_the_project_version_agree():
    """They are two hand-maintained strings for one number. Letting them
    drift means the upgrade reports one thing and pip installs another."""
    import re

    pkg = re.search(r'__version__ = "([^"]+)"',
                    (REPO_ROOT / "catalyst" / "__init__.py").read_text()).group(1)
    proj = re.search(r'^version = "([^"]+)"',
                     (REPO_ROOT / "pyproject.toml").read_text(),
                     re.M).group(1)
    assert pkg == proj, f"catalyst.__version__ is {pkg}, pyproject says {proj}"


# ==========================================================================
# The suite must be hermetic, not merely offline.
#
# Reported 2026-08-11: one test passed here and failed on the owner's
# server, failing the upgrade gate. Cause: conftest used
# os.environ.setdefault for CATALYST_CREDENTIALS and CATALYST_DB, which
# KEEPS whatever the environment already holds - on an installed machine
# the real /etc/catalyst/credentials.json. So the "fully offline" suite
# was reading live credentials and its results depended on the machine.
#
# Same defect as the bar-cache one before it, and the same lesson: a
# test whose result depends on the machine is not a test.
# ==========================================================================


class TestTheSuiteCannotReachTheInstalledSystem:
    @pytest.mark.parametrize("var", ["CATALYST_CREDENTIALS", "CATALYST_DB",
                                     "CATALYST_LOCK", "CATALYST_BARS"])
    def test_no_path_variable_can_reach_the_installed_system(self, var):
        """A test may legitimately repoint these at its OWN temp dir.
        What none of them may ever do is address the real installation."""
        import os
        import tempfile

        value = os.environ.get(var, "")
        assert value, f"{var} must be pinned by conftest, not left to chance"
        for real in ("/etc/catalyst", "/var/lib/catalyst", "/var/backups"):
            assert not value.startswith(real), (
                f"{var}={value!r} addresses the installed system")
        assert value.startswith(tempfile.gettempdir()), (
            f"{var}={value!r} is outside any temporary directory")

    def test_conftest_assigns_rather_than_defaulting(self):
        """setdefault is the specific mistake: it silently exempts a
        variable from the isolation rule stated two lines above it, so
        an installed machine's own paths survive into the suite."""
        text = (REPO_ROOT / "tests" / "conftest.py").read_text()
        block = text.split("sandbox = ")[1]
        code = "\n".join(ln for ln in block.splitlines()
                          if not ln.strip().startswith("#"))
        for var in ("CATALYST_CREDENTIALS", "CATALYST_DB", "CATALYST_LOCK",
                    "CATALYST_BARS"):
            assert f'os.environ["{var}"]' in code, (
                f"{var} must be ASSIGNED; setdefault keeps the installed "
                "machine's own value")
        assert "setdefault" not in code

    def test_loading_credentials_in_a_test_never_reaches_a_real_file(self):
        import tempfile

        from catalyst.setup.credentials import credentials_path

        path = str(credentials_path())
        assert path.startswith(tempfile.gettempdir())
        assert not path.startswith("/etc/")


class TestEveryDataFileActuallyShips:
    """A .sql file is not a .py file and is not installed by default.
    That was found once and fixed by listing directories BY HAND - so
    when dashboard/schema_logs.sql arrived later it was never added and
    never shipped.

    The owner ran for weeks with an installed dashboard missing it, and
    because the build hash covers .sql files, the installed copy hashed
    differently from every released version. That is what sent us hunting
    for a phantom second checkout (2026-08-11).
    """

    def _declared_globs(self) -> list:
        import re

        text = (REPO_ROOT / "pyproject.toml").read_text()
        after = text.split("[tool.setuptools.package-data]")[1]
        # Stop at the NEXT section header, not at the first "[" - which
        # is the opening bracket of the value itself.
        block = re.split(r"^\[", after, maxsplit=1, flags=re.M)[0]
        return re.findall(r'"([^"]+)"', block)

    def test_every_sql_file_in_the_tree_is_covered_by_a_declared_glob(self):
        import fnmatch

        globs = self._declared_globs()
        assert globs, "package-data declares nothing"
        missing = []
        for path in sorted((REPO_ROOT / "catalyst").rglob("*.sql")):
            rel = path.relative_to(REPO_ROOT / "catalyst").as_posix()
            if not any(fnmatch.fnmatch(rel, g) or
                       fnmatch.fnmatch(rel, g.replace("**/", ""))
                       for g in globs):
                missing.append(rel)
        assert not missing, (
            f"these .sql files would not be installed: {missing}. "
            "They exist in the repo, so the tests pass and the installed "
            "copy is broken - the one difference the suite cannot see.")

    def test_the_declaration_is_a_recursive_glob_not_a_hand_list(self):
        """A hand-written list of directories is a thing to remember, and
        it was forgotten once. A recursive glob ships a new file because
        it exists."""
        assert any("**" in g for g in self._declared_globs()), (
            "declare package data recursively, or the next .sql file "
            "added in a new directory silently fails to ship")

    def test_the_dashboard_log_schema_is_among_them(self):
        """The specific file that did not ship, named so a regression is
        legible rather than abstract."""
        assert (REPO_ROOT / "catalyst" / "dashboard" / "schema_logs.sql").exists()
        import fnmatch
        assert any(fnmatch.fnmatch("dashboard/schema_logs.sql", g)
                   for g in self._declared_globs())


class TestReRunningTheInstallerIsSafe:
    """The owner ran install.sh instead of upgrade.sh by accident
    (2026-08-11). Nothing was lost - the installer never overwrites
    credentials, never deletes a database and keeps the existing access
    code - but it installed and restarted with NO gate: no backup, no
    test suite, no rollback. On a machine holding positions that is the
    one thing that should not happen quietly."""

    def test_the_installer_hands_over_when_already_installed(self):
        text = INSTALL_SH.read_text()
        assert 'exec bash "${SCRIPT_DIR}/upgrade.sh"' in text
        assert "CATALYST_SKIP_PULL=1" in text, (
            "someone running the installer is asking to install THESE "
            "files, not to pull new ones behind their back")

    def test_it_only_hands_over_when_there_is_something_to_upgrade(self):
        """A first install has no venv and must not delegate, or a fresh
        machine can never be set up at all."""
        text = INSTALL_SH.read_text()
        block = text.split("CATALYST_NO_DELEGATE")[1].split("\nfi")[0]
        assert '[ -x "${VENV_PY}" ]' in block
        assert '[ -f "${SCRIPT_DIR}/upgrade.sh" ]' in block
        assert "rev-parse --git-dir" in block, (
            "the upgrade needs a git working copy; delegating without one "
            "would refuse an install that would otherwise have worked")

    def test_the_installer_still_never_overwrites_saved_state(self):
        """The property that made the accident harmless. Kept explicit so
        it cannot be lost in a later edit."""
        text = INSTALL_SH.read_text()
        assert "never overwrites credentials" in text
        assert "init_db only ever creates tables that are missing" in text

    def test_re_running_keeps_the_access_code(self, cred_file):
        """The code is in the link the owner uses. Regenerating it on a
        re-run would lock them out of their own dashboard."""
        from catalyst.setup.credentials import (
            ensure_dashboard_token, load_credentials, save_credentials,
        )
        save_credentials("PKAAAAAAAAAAAAAAAAAA", "ssssssssssssssssssss",
                         "sk-ant-aaaaaaaaaaaaaaaa", None,
                         anthropic_admin_key="sk-ant-admin01-keepme",
                         settings={"monthly_budget_usd": 20})
        before = load_credentials()
        ensure_dashboard_token()          # what the installer calls
        after = load_credentials()
        assert after.dashboard_token == before.dashboard_token
        assert after.anthropic_admin_key == before.anthropic_admin_key
        assert after.settings["monthly_budget_usd"] == 20
