"""Build stamp, and the manifest behind it.

Hard-won lesson (ui-designer): a stale browser and a failed deploy are
indistinguishable unless the page carries a hash the server can
contradict. BUILD_HASH is computed from the dashboard's own source at
import time, so a deployed-but-not-restarted service and a
cached-in-browser page both show up as a mismatch against /health.

SECOND LESSON, owner-reported 2026-08-11: the hash alone is a dead end.
The dashboard reported a build that matched no commit in any branch -
which proves the running files differ from every released version and
says nothing at all about HOW. A fingerprint with no provenance is
exactly what the rest of this dashboard refuses to print, and this
module was the one place still doing it.

So the manifest travels with the hash: which directory was hashed, which
files were found there, and each file's own digest and size. A hash that
matches nothing is then one glance from an answer - an extra file, a
missing file, or a file whose contents differ.
"""

import hashlib
from pathlib import Path

_DIR = Path(__file__).parent


def _hashed_files() -> list:
    """Every file that feeds the build hash, in hashing order."""
    return sorted(_DIR.glob("*.py")) + sorted(_DIR.glob("*.sql"))


def compute_build_hash() -> str:
    h = hashlib.sha256()
    for path in _hashed_files():
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


def build_manifest() -> dict:
    """What the hash was computed from, in enough detail to diagnose a
    mismatch without shell access to the machine.

    Never raises: a build stamp that can fail to render is worse than a
    wrong one, because it takes the page down with it.
    """
    files = []
    for path in _hashed_files():
        try:
            data = path.read_bytes()
            files.append({
                "name": path.name,
                "sha256": hashlib.sha256(data).hexdigest()[:12],
                "bytes": len(data),
            })
        except OSError as exc:
            files.append({"name": path.name, "sha256": f"unreadable: {exc}",
                          "bytes": -1})
    return {
        "build_hash": BUILD_HASH,
        "directory": str(_DIR),
        "file_count": len(files),
        "files": files,
    }


BUILD_HASH = compute_build_hash()
