"""Catalyst trading bot.

The one rule that is not negotiable: the model proposes, deterministic
code disposes. See docs/ARCHITECTURE.md for the interface contract every
module in this package implements.

THE VERSION, AND WHY IT LOOKS THE WAY IT DOES
---------------------------------------------
Two owner reports pull in opposite directions and both are right:

  "we arent updating build versions when we push via main all say 0.2.0"
  "the version numbering is crazy complicated why cant we have minor and
   major updated e.g. 0.2.0 to 0.3.2"

The first kills a hand-maintained number: nobody remembers to bump it,
so after an upgrade it actively says nothing changed - the precise shape
of the evening this project lost to a change that never reached the
machine. The second kills a twelve-character hex string: it moves, but
it is not a version, it is a fingerprint.

So the version is an ordinary `major.minor.patch`, and only the first
two are written by a person:

    0.3.14
    ^^^     catalyst/VERSION - the series. Bumped when a release is
            worth naming. The ONLY hand-set part.
       ^^   commits since VERSION last changed. Counted, never typed,
            so it moves on its own every single time code ships.

The commit stays available as `__build__`, beside the version rather
than glued into it. That is the number to quote when two machines
disagree about what they are running; the version is the number to read.
"""

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent
#: The release series, major.minor. Hand-set - the only part that is.
__release__ = (ROOT / "VERSION").read_text().strip()

#: Shown for a patch number that could not be counted. Deliberately not
#: a digit: a wrong count is indistinguishable from a right one, and
#: this is the number the owner reads to decide whether an upgrade
#: landed. Deliberately not "unknown" either - install/upgrade.sh treats
#: that exact string as "the package will not even start" and rolls back.
UNCOUNTED = "x"


def _run(*args: str) -> str:
    """git, or "" - never an exception and never a long wait. Everything
    here is a label; none of it may delay or block a trade."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _commit() -> str:
    """The short commit this code is actually running from.

    Three sources, in order of trustworthiness:

      1. CATALYST_BUILD_COMMIT / .build_commit, written by
         install/upgrade.sh at deploy time - authoritative on the VPS,
         where the installed copy in site-packages has no .git at all
      2. `git rev-parse` - the developer case, +dirty when the working
         tree does not match the commit
      3. "unknown" - said plainly. Never a plausible-looking fake.
    """
    stamped = os.environ.get("CATALYST_BUILD_COMMIT")
    if stamped:
        return stamped.strip()[:12]
    repo = ROOT.parent
    try:
        marker = repo / ".build_commit"
        if marker.exists():
            text = marker.read_text().strip()
            if text:
                return text[:12]
    except OSError:
        pass
    head = _run("git", "-C", str(repo), "rev-parse", "--short=12", "HEAD")
    if head:
        dirty = _run("git", "-C", str(repo), "status", "--porcelain")
        return head + ("+dirty" if dirty else "")
    return "unknown"


def _patch() -> str:
    """Commits since catalyst/VERSION last changed.

    Same three-source shape as _commit(), for the same reason: the
    installed copy on the VPS has no git, so upgrade.sh counts this
    where the repository actually is and stamps the answer.

    A shallow clone cannot see when VERSION changed. It falls back to
    counting the whole history, which is wrong in value but right in
    behaviour - it still increases by one per commit, which is the only
    property that matters for "did my upgrade land".
    """
    stamped = os.environ.get("CATALYST_BUILD_NUMBER")
    if stamped and stamped.strip().isdigit():
        return stamped.strip()
    repo = ROOT.parent
    try:
        marker = repo / ".build_number"
        if marker.exists():
            text = marker.read_text().strip()
            if text.isdigit():
                return text
    except OSError:
        pass
    base = _run("git", "-C", str(repo), "log", "-1", "--format=%H",
                "--", str(ROOT / "VERSION"))
    span = f"{base}..HEAD" if base else "HEAD"
    count = _run("git", "-C", str(repo), "rev-list", "--count", span)
    return count if count.isdigit() else UNCOUNTED


#: What upgrade.sh prints and the dashboard shows. major.minor.patch,
#: and the patch moves by itself on every commit.
__version__ = f"{__release__}.{_patch()}"

#: The exact code, for when two machines disagree about what they run.
#: Beside the version, never inside it.
__build__ = _commit()
