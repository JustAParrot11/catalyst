"""Source-text guards: path-independent, subprocess-free, non-vacuous.

WHY THIS MODULE EXISTS. On 2026-09-13 the owner's upgrade FAILED and
auto-rolled back. The failing test was mine:

    subprocess.run(["grep", "-rn", "_fetch_one_comparison_now",
                    "catalyst/risk", "catalyst/execution", "catalyst/cost"],
                   capture_output=True, text=True,
                   cwd="/home/user/catalyst")
    assert out.stdout.strip() == ""

`/home/user/catalyst` is the path of the sandbox it was written in. The
owner's repo is somewhere else, so `subprocess.run` raised
FileNotFoundError, one test of 4120 failed, and upgrade.sh correctly
refused to put that version anywhere near money.

Three defects, and the crash is the least of them:

1. **A hard-coded absolute path.** The repo root is derivable from
   `__file__` and nothing else may ever be used.
2. **Shelling out to `grep`** for four lines of Python. grep's presence,
   its exit codes and its path resolution are three failure modes a
   test does not need, and none of them is the thing under test.
3. **THE VACUOUS PASS**, which is the real defect. The assertion was
   `stdout == ""`. On any machine where those paths failed to resolve
   *without* raising - a partial checkout, a renamed package, a run
   from the wrong directory - the guard passed while searching nothing.
   A test that cannot fail is not a test (house rule 4), and this one
   could only fail by crashing.

So every search here **proves its own haystack first**: the directory
must exist and must contain at least one `.py` file, or the call raises
with the resolved path in the message. An empty result then means
"searched and found nothing", never "searched nothing".
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def python_files(*relative_dirs: str) -> list[Path]:
    """Every .py file under each directory, haystack proven non-empty.

    Raises rather than returning [] when a directory is missing or holds
    no Python, because an empty haystack makes every "nothing matched"
    assertion vacuously true.
    """
    if not relative_dirs:
        raise AssertionError("no directory to search - name at least one")
    found: list[Path] = []
    for relative in relative_dirs:
        directory = REPO_ROOT / relative
        if not directory.is_dir():
            raise AssertionError(
                f"cannot search {relative!r}: {directory} is not a "
                f"directory. The repo root resolved to {REPO_ROOT}, from "
                f"{__file__}. A guard that cannot find its haystack must "
                f"fail loudly, never pass by searching nothing.")
        here = [path for path in sorted(directory.rglob("*.py"))
                if "__pycache__" not in path.parts]
        if not here:
            raise AssertionError(
                f"cannot search {relative!r}: {directory} holds no .py "
                f"files, so anything asserted about its contents would be "
                f"vacuously true.")
        found.extend(here)
    return found


def _lines(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    return enumerate(text.splitlines(), 1)


def _hit(path: Path, number: int, line: str) -> str:
    return f"{path.relative_to(REPO_ROOT).as_posix()}:{number}: {line.strip()}"


def source_matches(needle: str, *relative_dirs: str) -> list[str]:
    """Every line under those directories containing `needle`.

    The pure-Python equivalent of `grep -rn --include='*.py'`, returning
    "<path relative to the repo root>:<line>: <text>" so a failure names
    the offending file wherever the repo happens to live.
    """
    if not needle:
        raise AssertionError("an empty needle matches every line")
    return [_hit(path, number, line)
            for path in python_files(*relative_dirs)
            for number, line in _lines(path)
            if needle in line]


def pattern_matches(pattern: str, *relative_paths: str) -> list[str]:
    """Every line in those FILES matching `pattern` (`re`, not grep -E).

    Takes files rather than directories because that is what the callers
    need; each one must exist, for the same reason as above.
    """
    compiled = re.compile(pattern)
    hits: list[str] = []
    for relative in relative_paths:
        path = REPO_ROOT / relative
        if not path.is_file():
            raise AssertionError(
                f"cannot search {relative!r}: {path} is not a file. The "
                f"repo root resolved to {REPO_ROOT}. A guard that cannot "
                f"find its haystack must fail loudly.")
        hits.extend(_hit(path, number, line)
                    for number, line in _lines(path)
                    if compiled.search(line))
    return hits
