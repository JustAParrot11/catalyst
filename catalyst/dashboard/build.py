"""Build stamp.

Hard-won lesson (ui-designer): a stale browser and a failed deploy are
indistinguishable unless the page carries a hash the server can
contradict. BUILD_HASH is computed from the dashboard's own source at
import time, so a deployed-but-not-restarted service and a
cached-in-browser page both show up as a mismatch against /health.
"""

import hashlib
from pathlib import Path

_DIR = Path(__file__).parent


def compute_build_hash() -> str:
    h = hashlib.sha256()
    for path in sorted(_DIR.glob("*.py")) + sorted(_DIR.glob("*.sql")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


BUILD_HASH = compute_build_hash()
