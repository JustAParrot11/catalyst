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

#: THE STAMP THAT ACTUALLY REACHES THE RUNNING BOT, and the reason this
#: file was wrong twice.
#:
#: OWNER-REPORTED: "almost it does just say 0.3.x though?" - and they
#: were looking at a correctly upgraded machine. Every other source
#: silently cannot exist in the place the bot actually runs:
#:
#:   CATALYST_BUILD_* - install/catalyst.service sets no such variable,
#:     and adding one would only stamp the service, not a shell, a cron
#:     job or the diagnostic bundle.
#:   .build_commit / .build_number at the repository root - written
#:     there, but the service imports from site-packages, whose parent
#:     is site-packages. It never sees the repository at all.
#:   git - site-packages has no .git.
#:
#: So all three fell through to "unknown" and "x" on the one machine
#: that matters, while upgrade.sh's own printout looked right because
#: IT passes the variable explicitly. A number that is correct
#: everywhere except in front of the owner is not a number.
#:
#: This file ships INSIDE the package, so it travels with the code it
#: describes. Two lines: the patch count, then the commit.
STAMP = ROOT / "BUILD"


def _run(*args: str) -> str:
    """git, or "" - never an exception and never a long wait. Everything
    here is a label; none of it may delay or block a trade."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _stamped(line: int) -> str:
    """One line of the shipped stamp, or "". Never raises: a missing or
    unreadable stamp is a label falling back, not a failure."""
    try:
        if not STAMP.exists():
            return ""
        lines = STAMP.read_text().splitlines()
    except OSError:
        return ""
    return lines[line].strip() if len(lines) > line else ""


#: SOURCE ORDER, and it is the same for both numbers.
#:
#:   1. CATALYST_BUILD_* - an explicit override, for a caller that
#:      already knows (upgrade.sh, checking what it just installed).
#:   2. .build_* at the REPOSITORY root - a developer's own checkout.
#:   3. git - a real checkout is the truth, and it is always current,
#:      so it must beat any stamp lying about in the tree.
#:   4. catalyst/BUILD - shipped inside the package. Only reachable
#:      when git is not, which is exactly the installed case, and it is
#:      the ONLY source that survives into site-packages.
#:   5. Said plainly: "unknown" / "x". Never a plausible-looking fake.
#:
#: git ABOVE the shipped stamp on purpose: in a working checkout the
#: stamp can be stale from an earlier install, and stale is the failure
#: this whole file exists to prevent.


def _commit() -> str:
    """The short commit this code is actually running from."""
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
    return _stamped(1)[:12] or "unknown"


def _patch() -> str:
    """Commits since catalyst/VERSION last changed.

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
    if count.isdigit():
        return count
    shipped = _stamped(0)
    return shipped if shipped.isdigit() else UNCOUNTED


#: What upgrade.sh prints and the dashboard shows. major.minor.patch,
#: and the patch moves by itself on every commit.
__version__ = f"{__release__}.{_patch()}"

#: The exact code, for when two machines disagree about what they run.
#: Beside the version, never inside it.
__build__ = _commit()
