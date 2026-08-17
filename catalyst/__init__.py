"""Catalyst trading bot.

The one rule that is not negotiable: the model proposes, deterministic
code disposes. See docs/ARCHITECTURE.md for the interface contract every
module in this package implements.
"""

#: The release series. Hand-maintained, and deliberately NOT the thing
#: anyone checks after an upgrade.
__release__ = "0.2"


def _commit() -> str:
    """The short commit this code is actually running from.

    OWNER-REPORTED: "we arent updating build versions when we push via
    main all say 0.2.0". They were right, and a hand-maintained string
    was always going to do that - CLAUDE.md already warned "the version
    string is not the signal, it is hand-maintained and sits still
    across real changes". A number nobody remembers to bump is worse
    than no number: it actively tells the owner nothing changed.

    So it is derived. Three sources, in order of trustworthiness:

      1. BUILD_COMMIT written by install/upgrade.sh at deploy time -
         authoritative on the VPS, where there may be no git at all
      2. `git rev-parse` - the developer case
      3. "unknown" - said plainly, never faked

    Never raises and never blocks a trade: this is a label.
    """
    import os
    import pathlib
    import subprocess

    stamped = os.environ.get("CATALYST_BUILD_COMMIT")
    if stamped:
        return stamped.strip()[:12]
    root = pathlib.Path(__file__).resolve().parent.parent
    marker = root / ".build_commit"
    try:
        if marker.exists():
            text = marker.read_text().strip()
            if text:
                return text[:12]
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            dirty = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True, text=True, timeout=5)
            suffix = "+dirty" if dirty.stdout.strip() else ""
            return out.stdout.strip() + suffix
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


#: What upgrade.sh prints and the dashboard shows. Moves on every commit,
#: because it IS the commit.
__version__ = f"{__release__}+{_commit()}"
