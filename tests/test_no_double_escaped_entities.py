"""An HTML entity that reaches the browser as text is a rendering bug.

FOUND BY RENDERING, not by reading. The new data-integrity page carried
this line:

    "That is not a good result, it is the absence of a measurement
     &mdash; the modelled column is ..."

and the browser showed a literal `&mdash;` mid-sentence, because
`caveat()` escapes its argument and `esc("&mdash;")` is `&amp;mdash;`.
The same mistake in a heading also survives `.upper()` as a visible
`&AMP;MDASH;`, which is how this class was first noticed in this repo.

The trap is that BOTH forms are correct somewhere:

    raw HTML string        "&mdash;"   -> renders as an em dash
    argument to esc()      DASH        -> renders as an em dash
    argument to esc()      "&mdash;"   -> renders as the TEXT "&mdash;"

so no single rule about which to write catches it. What catches it is
looking at the output, which is what this file does: it renders every
route and asserts that no doubly-escaped entity reached the page.

It is deliberately a WHOLE-PAGE sweep rather than a check on one panel.
The defect is a class, it costs nothing to reintroduce, and it is
invisible to every test that asserts on the presence of words.
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from catalyst.dashboard.db import Db
from catalyst.dashboard.server import HTML_ROUTES
from catalyst.storage import init_db

SCHEMA_LOGS = (Path(__file__).resolve().parent.parent / "catalyst" /
               "dashboard" / "schema_logs.sql")

#: `&amp;` followed by anything that looks like an entity name. Matches
#: the `&AMP;MDASH;` upper-cased form too, which is the one that first
#: appeared in a heading here.
DOUBLE_ESCAPED = re.compile(r"&amp;[a-zA-Z]{2,10};|&AMP;[A-Za-z]{2,10};")

#: Sections of the page that are SUPPOSED to show markup as text: the
#: dashboard prints raw upstream responses beside every zero (house
#: rule 3), and a JSON body legitimately containing "&amp;mdash;" is
#: evidence, not a bug.
RAW_BLOCK = re.compile(r"<(pre|code|script|style)\b.*?</\1>", re.S)


@pytest.fixture
def db(tmp_path):
    """Enough rows that most panels render something rather than their
    empty state - an empty page cannot carry this defect."""
    now = datetime.now(timezone.utc)
    day = now.date()
    iso = now.isoformat()
    path = str(tmp_path / "d.db")
    conn = init_db(path)
    conn.executescript(SCHEMA_LOGS.read_text())
    conn.execute("INSERT INTO raw_events VALUES (?,?,?,?)",
                 ("edgar_form4", "acc-1", iso, '{"form":"4"}'))
    conn.execute("INSERT INTO raw_events_errors VALUES (?,?,?)",
                 ("federal_register", iso, '{"status":500,"body":"timeout"}'))
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 ("c1", "ACME", "insider_cluster",
                  (day + timedelta(days=6)).isoformat(), "confirmed",
                  json.dumps(["acc-1"]), iso, "industrials",
                  json.dumps(["ind"])))
    conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                 ("c1", "long", 0.8, "insiders bought", "the readout misses",
                  12, 0, "no move yet"))
    conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("o1", "c1", "b1", "buy", "4", "market", "day", iso,
                  "filled", "{}"))
    conn.execute("INSERT INTO entry_market_context VALUES (?,?,?,?)",
                 ("o1", "4.2", "37.00", iso))
    conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?)",
                 ("o1", "37.0400", "4", iso, "37.0400", "0.6100"))
    conn.commit()
    conn.close()
    return path


def _pages(path):
    """Every HTML route, rendered. Params are empty: the defect lives in
    prose the page always prints, not in a particular query string."""
    out = {}
    for route, handler in sorted(HTML_ROUTES.items()):
        handle = Db(path)
        try:
            out[route] = handler(handle, {})
        except Exception as exc:      # noqa: BLE001 - reported, not hidden
            out[route] = f"__RAISED__ {type(exc).__name__}: {exc}"
        finally:
            handle.close()
    return out


class TestNoEntityReachesTheBrowserAsText:
    def test_every_route_renders_at_all(self, db):
        """The sweep below would pass trivially on a page that raised."""
        for route, html in _pages(db).items():
            assert not html.startswith("__RAISED__"), f"{route}: {html}"
            assert len(html) > 200, f"{route} rendered {len(html)} bytes"

    def test_no_route_shows_a_doubly_escaped_entity(self, db):
        offenders = []
        for route, html in _pages(db).items():
            visible = RAW_BLOCK.sub(" ", html)
            for match in DOUBLE_ESCAPED.finditer(visible):
                start = max(0, match.start() - 70)
                offenders.append(
                    f"{route}: ...{visible[start:match.end() + 40]}...")
        assert not offenders, (
            "these render in the browser as literal entity text:\n" +
            "\n".join(offenders))

    def test_the_integrity_page_uses_a_real_em_dash(self, db):
        """The specific instance that produced this file."""
        page = _pages(db)["/integrity"]
        assert "absence of a measurement" in page
        i = page.find("absence of a measurement")
        assert "—" in page[i:i + 60], (
            "the em dash after 'absence of a measurement' is not a real "
            "character")


class TestThisCheckCanFail:
    """House rule 4. The regex above is the whole test, so it is the
    thing that has to be shown to catch something."""

    def test_the_pattern_matches_what_the_bug_looks_like(self):
        for bad in ("a &amp;mdash; b", "A &AMP;MDASH; B",
                    "x&amp;nbsp;y", "&amp;middot;"):
            assert DOUBLE_ESCAPED.search(bad), bad

    def test_the_pattern_does_not_match_correct_output(self):
        for good in ("a &mdash; b", "—", "Jones &amp; Sons",
                     "R&amp;D spending", "a &amp; b", "&amp;"):
            assert not DOUBLE_ESCAPED.search(good), good

    def test_a_planted_offender_is_caught(self, db, monkeypatch):
        """Break a copy: put the bug back into the panel and confirm the
        sweep reports it."""
        from catalyst.dashboard import panels

        real = panels.data_integrity_panel

        def broken(*a, **kw):
            return real(*a, **kw).replace(
                "absence of a measurement", "absence &amp;mdash; of one")

        monkeypatch.setattr(panels, "data_integrity_panel", broken)
        offenders = []
        for route, html in _pages(db).items():
            if DOUBLE_ESCAPED.search(RAW_BLOCK.sub(" ", html)):
                offenders.append(route)
        assert "/integrity" in offenders, (
            "the sweep did not notice a planted doubly-escaped entity")
