"""The suite must not read the installed machine's own files.

This lives in its OWN module on purpose. test_install.py has an autouse
fixture that monkeypatches CATALYST_CREDENTIALS for every test in it, so
a leak test placed there can never fail - the fixture puts the value
back regardless of whether the backstop exists. That is exactly how the
first version of these tests passed against a sabotage.

The failure being guarded against, twice reported by the owner: a test
set CATALYST_CREDENTIALS itself and "cleaned up" with os.environ.pop,
which DELETES the sandbox pin rather than restoring it. Every test after
it fell back to the real /etc/catalyst/credentials.json - invisible on a
machine without one, and a live $20 budget on the owner's server, which
failed the upgrade gate and rolled the release back.
"""

import os
import tempfile

import pytest

PINNED = ("CATALYST_LOCK", "CATALYST_DB", "CATALYST_CREDENTIALS",
          "CATALYST_BARS")


@pytest.mark.parametrize("var", PINNED)
def test_no_path_variable_can_reach_the_installed_system(var):
    """A test may legitimately repoint these at its OWN temp dir. What
    none of them may ever do is address the real installation."""
    value = os.environ.get(var, "")
    assert value, f"{var} must be pinned by conftest, not left to chance"
    for real in ("/etc/catalyst", "/var/lib/catalyst", "/var/backups"):
        assert not value.startswith(real), (
            f"{var}={value!r} addresses the installed system")
    assert value.startswith(tempfile.gettempdir()), (
        f"{var}={value!r} is outside any temporary directory")


def test_conftest_assigns_rather_than_defaulting():
    """setdefault silently exempts a variable from the isolation rule
    stated directly above it, keeping the installed machine's paths."""
    from pathlib import Path

    text = (Path(__file__).resolve().parent / "conftest.py").read_text()
    block = text.split("sandbox = ")[1]
    code = "\n".join(ln for ln in block.splitlines()
                     if not ln.strip().startswith("#"))
    for var in PINNED:
        assert f'os.environ["{var}"]' in code, (
            f"{var} must be ASSIGNED; setdefault keeps the installed "
            "machine's own value")
    assert "setdefault" not in code


def test_a_test_that_deletes_the_pin_cannot_poison_the_next_one():
    """Does the damage on purpose. The autouse backstop in conftest must
    put the pin back before the next test runs."""
    for var in PINNED:
        os.environ.pop(var, None)
    assert "CATALYST_CREDENTIALS" not in os.environ


@pytest.mark.parametrize("var", PINNED)
def test_the_pin_is_back_after_the_test_that_deleted_it(var):
    """Runs after the one above, by file order. Without the backstop
    this sees nothing at all - which is precisely the state in which
    catalyst then reads /etc/catalyst/credentials.json."""
    value = os.environ.get(var, "")
    assert value, f"{var} was deleted by an earlier test and never restored"
    assert value.startswith(tempfile.gettempdir())


def test_loading_credentials_here_never_resolves_to_a_real_file():
    from catalyst.setup.credentials import credentials_path

    path = str(credentials_path())
    assert path.startswith(tempfile.gettempdir())
    assert not path.startswith("/etc/")
