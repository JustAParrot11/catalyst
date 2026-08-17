"""The version must move when the code moves.

OWNER-REPORTED: "we arent updating build versions when we push via main
all say 0.2.0".

They were right, and CLAUDE.md had already warned about it in as many
words - "the version string is not the signal, it is hand-maintained and
sits still across real changes". A number nobody remembers to bump is
worse than no number at all: after an upgrade it actively tells the
owner nothing changed, which is the precise shape of the evening this
project has already lost to a change that never reached the machine.

So it is DERIVED, from three sources in order of trustworthiness:

  1. CATALYST_BUILD_COMMIT / .build_commit, written by upgrade.sh at
     deploy time. Authoritative on the VPS, where the service user may
     have no git and site-packages has no .git at all.
  2. `git rev-parse`, for the developer case, with +dirty when the
     working tree is not clean.
  3. "unknown", said plainly. Never a plausible-looking fake.

It is a LABEL. It must never raise, never block, and never take long -
several tests below exist only to keep that true.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def version_with(env=None, cwd=None):
    """A fresh interpreter, so module-level caching cannot mask this."""
    e = dict(os.environ)
    e.pop("CATALYST_BUILD_COMMIT", None)
    e.update(env or {})
    e["PYTHONPATH"] = str(ROOT)
    out = subprocess.run(
        [sys.executable, "-c", "import catalyst; print(catalyst.__version__)"],
        capture_output=True, text=True, env=e, cwd=cwd or str(ROOT), timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


class TestItIsNoLongerAHandMaintainedConstant:
    def test_it_is_not_the_bare_release_string(self):
        import catalyst

        assert catalyst.__version__ != catalyst.__release__, (
            "the version is the release string again, so it will sit "
            "still across every commit exactly as it did before")

    def test_it_carries_the_commit(self):
        import catalyst

        head = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=10)
        if head.returncode != 0:
            pytest.skip("not a git checkout")
        assert head.stdout.strip() in catalyst.__version__

    def test_a_different_commit_gives_a_different_version(self):
        """The whole point. Two deploys of different code must not print
        the same string."""
        a = version_with({"CATALYST_BUILD_COMMIT": "aaaaaaaaaaaa"})
        b = version_with({"CATALYST_BUILD_COMMIT": "bbbbbbbbbbbb"})
        assert a != b
        assert "aaaaaaaaaaaa" in a and "bbbbbbbbbbbb" in b


class TestTheDeployStampWins:
    def test_an_explicit_stamp_overrides_git(self):
        """On the VPS the installed package has no .git. upgrade.sh
        knows the commit and writes it down; that must be believed."""
        v = version_with({"CATALYST_BUILD_COMMIT": "deadbeefcafe"})
        assert "deadbeefcafe" in v

    def test_a_stamp_is_truncated_not_trusted_wholesale(self):
        v = version_with({"CATALYST_BUILD_COMMIT": "x" * 200})
        assert len(v) < 40, f"an absurd stamp became the version: {v!r}"

    def test_EVERY_install_is_preceded_by_a_stamp(self):
        """Order matters: pip copies the tree into site-packages, so a
        stamp written afterwards never reaches the installed copy.

        And there are TWO installs, which is how this test earned its
        keep. The upgrade path installs the new commit; the ROLLBACK
        path reinstalls the old one. Stamping only the first leaves a
        rolled-back machine reporting the version it failed to install
        while running the previous code - a lie in precisely the number
        the owner checks to decide whether an upgrade landed.
        """
        sh = (ROOT / "install" / "upgrade.sh").read_text()
        lines = sh.splitlines()
        installs = [i for i, ln in enumerate(lines)
                    if 'pip install --quiet "${REPO_DIR}[dev]"' in ln]
        stamps = [i for i, ln in enumerate(lines) if ".build_commit" in ln]
        assert len(installs) >= 2, (
            "expected an upgrade install and a rollback reinstall")
        for i in installs:
            preceding = [t for t in stamps if t < i]
            assert preceding, (
                f"the install on line {i + 1} has no commit stamp before "
                "it, so the installed package cannot report its commit")
            assert i - max(preceding) < 20, (
                f"the nearest stamp to the install on line {i + 1} is "
                f"{i - max(preceding)} lines away - too far to be the "
                "one that belongs to it")

    def test_the_stamp_file_is_not_committed(self):
        """It is per-deploy, and committing it would make every machine
        claim whichever commit was last stamped by a developer."""
        assert ".build_commit" in (ROOT / ".gitignore").read_text()


class TestItIsOnlyALabel:
    def test_it_never_raises_outside_a_git_checkout(self, tmp_path):
        """site-packages has no .git. Importing catalyst there must not
        fail, and must not print a fake commit either."""
        v = version_with(cwd=str(tmp_path))
        assert v, "importing catalyst outside a checkout produced nothing"

    def test_an_absent_commit_is_admitted_not_invented(self, tmp_path):
        """"unknown" is a fact. A plausible-looking hex string would be
        a lie, and the owner checks this number to decide whether an
        upgrade landed."""
        import catalyst

        fake_root = tmp_path / "catalyst"
        fake_root.mkdir()
        (fake_root / "__init__.py").write_text(
            (ROOT / "catalyst" / "__init__.py").read_text())
        out = subprocess.run(
            [sys.executable, "-c",
             "import catalyst; print(catalyst._commit())"],
            capture_output=True, text=True, cwd=str(tmp_path),
            env={**os.environ, "PYTHONPATH": str(tmp_path),
                 "PATH": "/nonexistent"}, timeout=30)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "unknown", out.stdout

    def test_it_is_fast(self):
        """Two subprocess calls sit behind this. If it ever became slow
        it would be slow on every import, including the trading loop's."""
        import time

        import catalyst

        start = time.monotonic()
        catalyst._commit()
        assert time.monotonic() - start < 6.0


class TestTheOwnerCanTellTwoDeploysApart:
    def test_a_dirty_tree_says_so(self):
        """A developer running uncommitted code must not see a clean
        commit string and believe the machine matches the repo."""
        head = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10)
        if head.returncode != 0:
            pytest.skip("not a git checkout")
        import catalyst

        if head.stdout.strip():
            assert "+dirty" in catalyst.__version__
        else:
            assert "+dirty" not in catalyst.__version__

    def test_upgrade_prints_the_derived_version(self):
        sh = (ROOT / "install" / "upgrade.sh").read_text()
        assert "catalyst.__version__" in sh
        assert "NEW_VERSION" in sh
