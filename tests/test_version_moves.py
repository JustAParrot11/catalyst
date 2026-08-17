"""The version must be READABLE, and it must MOVE when the code moves.

Two owner reports, pulling opposite ways, and both are right:

  1. "we arent updating build versions when we push via main all say
     0.2.0" - a hand-maintained number nobody remembers to bump is worse
     than none: after an upgrade it actively says nothing changed, which
     is the precise shape of the evening this project has already lost
     to a change that never reached the machine.

  2. "the version numbering is crazy complicated why cant we have minor
     and major updated e.g. 0.2.0 to 0.3.2" - a twelve-character hex
     string moves, but it is not a version, it is a fingerprint.

The answer satisfies both: an ordinary major.minor.patch where only the
series is written by a person.

    0.3.14
    ^^^     catalyst/VERSION, hand-set, the ONLY hand-set part
       ^^   commits since VERSION last changed - counted, never typed

The commit survives as __build__, beside the version rather than inside
it. Both are LABELS: they must never raise, never block, never be slow -
several tests below exist only to keep that true.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def fresh(expr, env=None, cwd=None):
    """A fresh interpreter, so module-level caching cannot mask this."""
    e = dict(os.environ)
    e.pop("CATALYST_BUILD_COMMIT", None)
    e.pop("CATALYST_BUILD_NUMBER", None)
    e.update(env or {})
    e["PYTHONPATH"] = str(ROOT)
    out = subprocess.run(
        [sys.executable, "-c", f"import catalyst; print({expr})"],
        capture_output=True, text=True, env=e, cwd=cwd or str(ROOT),
        timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


class TestItReadsLikeAVersion:
    def test_it_is_major_minor_patch(self):
        import catalyst

        assert re.fullmatch(r"\d+\.\d+\.(\d+|x)", catalyst.__version__), (
            f"{catalyst.__version__!r} is not major.minor.patch - the "
            "owner asked for '0.2.0 to 0.3.2', not a fingerprint")

    def test_the_commit_hash_is_no_longer_glued_into_it(self):
        """THE COMPLAINT, directly. A version with a hash in it is two
        different numbers wearing one label."""
        import catalyst

        assert catalyst.__build__ not in catalyst.__version__
        assert "+" not in catalyst.__version__

    def test_the_commit_is_still_available_beside_it(self):
        """Removing it entirely would cost the one number that settles
        'are these two machines running the same code'."""
        import catalyst

        assert catalyst.__build__

    def test_only_the_series_is_hand_written(self):
        import catalyst

        assert (ROOT / "catalyst" / "VERSION").read_text().strip() \
            == catalyst.__release__
        assert catalyst.__version__.startswith(catalyst.__release__ + ".")

    def test_the_packaging_metadata_cannot_drift_from_it(self):
        """A second hand-typed copy of the series in pyproject.toml is
        exactly the reported failure again: two numbers, one stale, and
        no way to tell which."""
        text = (ROOT / "pyproject.toml").read_text()
        assert 'dynamic = ["version"]' in text
        assert 'version = {file = "catalyst/VERSION"}' in text
        assert not re.search(r'^version\s*=\s*"', text, re.M), (
            "pyproject.toml has a literal version again")


class TestThePatchMovesByItself:
    def test_it_is_a_count_not_a_constant(self):
        a = fresh("catalyst.__version__", {"CATALYST_BUILD_NUMBER": "7"})
        b = fresh("catalyst.__version__", {"CATALYST_BUILD_NUMBER": "8"})
        assert a != b and a.endswith(".7") and b.endswith(".8")

    def test_it_counts_commits_since_the_series_changed(self):
        """The count has to be SINCE the series, or bumping 0.3 -> 0.4
        would carry the whole history's count across with it."""
        import catalyst

        base = subprocess.run(
            ["git", "-C", str(ROOT), "log", "-1", "--format=%H", "--",
             "catalyst/VERSION"], capture_output=True, text=True, timeout=10)
        if base.returncode != 0 or not base.stdout.strip():
            pytest.skip("VERSION not committed yet")
        n = subprocess.run(
            ["git", "-C", str(ROOT), "rev-list", "--count",
             f"{base.stdout.strip()}..HEAD"],
            capture_output=True, text=True, timeout=10)
        assert catalyst.__version__.endswith("." + n.stdout.strip())

    def test_a_new_commit_gives_a_new_version(self):
        """The whole point. Two deploys of different code must not print
        the same string."""
        assert (fresh("catalyst.__version__", {"CATALYST_BUILD_NUMBER": "1"})
                != fresh("catalyst.__version__",
                         {"CATALYST_BUILD_NUMBER": "2"}))


class TestTheDeployStampWins:
    """On the VPS the installed package has no .git, so upgrade.sh counts
    where the repository is and writes the answer down."""

    @pytest.mark.parametrize("expr,env,want", [
        ("catalyst.__version__", {"CATALYST_BUILD_NUMBER": "41"}, ".41"),
        ("catalyst.__build__", {"CATALYST_BUILD_COMMIT": "deadbeefcafe"},
         "deadbeefcafe"),
    ])
    def test_an_explicit_stamp_overrides_git(self, expr, env, want):
        assert fresh(expr, env).endswith(want)

    def test_an_absurd_stamp_is_not_trusted_wholesale(self):
        assert len(fresh("catalyst.__build__",
                         {"CATALYST_BUILD_COMMIT": "x" * 200})) < 40

    def test_a_non_numeric_patch_stamp_is_ignored_not_printed(self):
        """A stamp is data from a shell script. If it ever carried an
        error message, printing it as the patch would put that message
        in front of the owner as a version number."""
        v = fresh("catalyst.__version__",
                  {"CATALYST_BUILD_NUMBER": "fatal: not a git repository"})
        assert "fatal" not in v
        assert re.fullmatch(r"\d+\.\d+\.(\d+|x)", v), v

    def test_EVERY_install_is_preceded_by_BOTH_stamps(self):
        """Order matters: pip copies the tree into site-packages, so a
        stamp written afterwards never reaches the installed copy.

        And there are TWO installs, which is how this test earned its
        keep. The upgrade path installs the new commit; the ROLLBACK
        path reinstalls the old one. Stamping only the first leaves a
        rolled-back machine reporting the version it failed to install
        while running the previous code - a lie in precisely the number
        the owner checks to decide whether an upgrade landed.
        """
        lines = (ROOT / "install" / "upgrade.sh").read_text().splitlines()
        installs = [i for i, ln in enumerate(lines)
                    if 'pip install --quiet "${REPO_DIR}[dev]"' in ln]
        assert len(installs) >= 2, (
            "expected an upgrade install and a rollback reinstall")
        for marker in (".build_commit", "stamp_build_number"):
            stamps = [i for i, ln in enumerate(lines)
                      if marker in ln and not ln.strip().startswith("#")
                      and "()" not in ln]
            for i in installs:
                preceding = [t for t in stamps if t < i]
                assert preceding, (
                    f"the install on line {i + 1} has no {marker} before "
                    "it, so the installed package cannot report itself")
                assert i - max(preceding) < 20, (
                    f"the nearest {marker} to the install on line {i + 1} "
                    f"is {i - max(preceding)} lines away - too far to be "
                    "the one that belongs to it")

    def test_the_helper_is_defined_before_rollback_uses_it(self):
        """Shell resolves a function at CALL time, so this would work by
        accident today and break the first time a rollback fires from
        earlier in the script - during a rollback, which is the one
        moment nothing else may go wrong."""
        text = (ROOT / "install" / "upgrade.sh").read_text()
        assert (text.index("stamp_build_number() {")
                < text.index("rollback() {"))

    @pytest.mark.parametrize("name", [".build_commit", ".build_number"])
    def test_the_stamp_files_are_not_committed(self, name):
        """They are per-deploy. Committing one would make every machine
        claim whichever build a developer stamped last."""
        assert name in (ROOT / ".gitignore").read_text()
        assert not (ROOT / name).exists() or subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--error-unmatch", name],
            capture_output=True).returncode != 0


class TestItIsOnlyALabel:
    def test_it_never_raises_outside_a_git_checkout(self, tmp_path):
        """site-packages has no .git. Importing catalyst there must not
        fail, and must not print a fake number either."""
        assert fresh("catalyst.__version__", cwd=str(tmp_path))

    def test_an_uncountable_patch_is_admitted_not_invented(self, tmp_path):
        """'x' is a fact. A plausible digit would be a lie, and this is
        the number the owner checks to decide whether an upgrade landed."""
        fake = tmp_path / "catalyst"
        fake.mkdir()
        (fake / "__init__.py").write_text(
            (ROOT / "catalyst" / "__init__.py").read_text())
        (fake / "VERSION").write_text("0.3\n")
        out = subprocess.run(
            [sys.executable, "-c",
             "import catalyst; print(catalyst.__version__, catalyst.__build__)"],
            capture_output=True, text=True, cwd=str(tmp_path),
            env={**os.environ, "PYTHONPATH": str(tmp_path),
                 "PATH": "/nonexistent"}, timeout=30)
        assert out.returncode == 0, out.stderr
        assert out.stdout.split() == ["0.3.x", "unknown"], out.stdout

    def test_the_uncountable_marker_is_never_the_word_upgrade_sh_fears(self):
        """install/upgrade.sh treats the exact string "unknown" as "the
        package will not even start" and ROLLS BACK. A version that said
        "unknown" because git was merely unavailable would undo a
        perfectly good upgrade."""
        import catalyst

        assert catalyst.UNCOUNTED != "unknown"
        sh = (ROOT / "install" / "upgrade.sh").read_text()
        assert '"${NEW_VERSION}" = "unknown"' in sh, (
            "the rollback trigger moved - re-check that an uncountable "
            "patch cannot trip it")

    def test_it_is_fast(self):
        """Subprocess calls sit behind this. If it ever became slow it
        would be slow on every import, including the trading loop's."""
        import time

        import catalyst

        start = time.monotonic()
        catalyst._patch()
        catalyst._commit()
        assert time.monotonic() - start < 8.0


class TestTheOwnerCanTellTwoDeploysApart:
    def test_a_dirty_tree_says_so(self):
        """A developer running uncommitted code must not see a clean
        commit string and believe the machine matches the repo."""
        st = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"],
                            capture_output=True, text=True, timeout=10)
        if st.returncode != 0:
            pytest.skip("not a git checkout")
        import catalyst

        assert ("+dirty" in catalyst.__build__) == bool(st.stdout.strip())

    def test_upgrade_prints_the_version_AND_the_build(self):
        sh = (ROOT / "install" / "upgrade.sh").read_text()
        assert "catalyst.__version__" in sh and "NEW_VERSION" in sh
        assert "catalyst.__build__" in sh and "NEW_BUILD" in sh

    def test_the_dashboard_leads_with_the_version(self):
        """The sidebar used to show only the hash, which is the number
        the owner cannot read. Both belong there, version first."""
        src = (ROOT / "catalyst" / "dashboard" / "render.py").read_text()
        # rindex, not index: the first 'sidebar-foot' is the CSS rule.
        foot = src[src.rindex('class="sidebar-foot'):][:600]
        assert "_VERSION" in foot and "_BUILD" in foot
        assert foot.index("_VERSION") < foot.index("BUILD_HASH")
