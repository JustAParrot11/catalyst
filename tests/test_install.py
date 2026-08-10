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
