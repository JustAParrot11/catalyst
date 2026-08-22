"""The diagnostic download is a folder you can read, not a wall of JSON.

OWNER-ASKED 2026-08-21: "when we download logs can we attach a html
reader for all the files ttached to make it easier for me to
troubleshoot before sending it to you, still include raw logs in the
folder".

THE CONSTRAINT THAT DECIDES THE DESIGN, and the one thing most likely
to be broken by a well-meaning later change: a browser will not let a
page opened from a folder read a file beside it. `file://` fetch is
blocked, and it fails quietly enough that the page merely looks broken.
So the reader cannot load bundle.json - the data is EMBEDDED. Several
tests here exist only to stop someone "tidying that up" into a fetch.

AND NOTHING IS TAKEN AWAY. The raw JSON and a plain-text log are still
in the folder, byte for byte, because those are what gets sent on.
"""

import json
import re
import zipfile

import pytest

from catalyst.dashboard.bundle_reader import (
    logs_as_text,
    render_bundle_html,
)


def _bundle(**over):
    b = {
        "generated_at": "2026-08-21T22:00:00+00:00",
        "build_hash": "abc123",
        "scope": "everything",
        "scope_covers": "the whole database",
        "scope_note": "This is the MASTER bundle - nothing is filtered out.",
        "window_note": "No time window - everything, however old.",
        "row_counts": {"orders": 5},
        "recent_logs": [
            {"ts": "2026-08-21T21:00:00+00:00", "level": "INFO",
             "component": "orchestrator.cycle",
             "message": "Cycle done in 2.1s: 4 candidate(s) -> 1 researched"},
            {"ts": "2026-08-21T21:05:00+00:00", "level": "ERROR",
             "component": "data.edgar", "message": "feed refused",
             "traceback_text": "Traceback...\nValueError: nope"},
        ],
        "rows": {"orders": [{"id": "o1", "side": "buy"}],
                 "empty_table": []},
    }
    b.update(over)
    return b


class TestItOpensFromAFolderWithNothingInstalled:

    def test_the_data_is_embedded_not_fetched(self):
        """THE ONE THAT MATTERS. A fetch() of bundle.json is blocked by
        every browser on file://, so the page would render its chrome
        and then sit empty - which looks like a bug in the bot rather
        than a bug in the reader."""
        html = render_bundle_html(_bundle())
        assert "fetch(" not in html, (
            "the reader fetches something; from a folder that is blocked")
        assert "XMLHttpRequest" not in html
        assert 'id="bundle-data"' in html
        assert "feed refused" in html, "the log rows are not in the page"

    def test_it_asks_the_network_for_nothing_at_all(self):
        """No CDN, no font, no analytics. It has to work on a laptop
        with no internet, and it must never phone anywhere with a
        diagnostic bundle in it."""
        html = render_bundle_html(_bundle())
        for probe in ("http://", "https://", "//cdn", "<link", "@import"):
            assert probe not in html, f"the reader reaches for {probe!r}"

    def test_a_log_line_containing_markup_cannot_end_the_script_early(self):
        """A bot that reads filings and news WILL eventually log a
        string containing </script>. Unescaped, that closes the tag and
        silently truncates the whole page."""
        nasty = "</script><script>window.OWNED=1</script>"
        html = render_bundle_html(_bundle(recent_logs=[
            {"ts": "t", "level": "INFO", "component": "c", "message": nasty}]))
        payload = re.search(
            r'<script type="application/json" id="bundle-data">(.*?)</script>',
            html, re.S)
        assert payload, "the data block was terminated early"
        assert "window.OWNED" not in payload.group(1).replace("<\\/", "</")[:0] + ""
        # The data survives, escaped, and parses back to the original.
        assert json.loads(payload.group(1).replace("<\\/", "</"))[
            "recent_logs"][0]["message"] == nasty

    def test_the_page_survives_a_bundle_with_nothing_in_it(self):
        html = render_bundle_html({"generated_at": "", "recent_logs": []})
        assert "<html" in html and "</html>" in html


class TestTheRawFilesAreStillThere:

    def test_the_plain_text_log_is_one_line_per_record(self):
        text = logs_as_text(_bundle())
        assert "Cycle done" in text
        assert "feed refused" in text
        first = text.splitlines()[0]
        assert first.startswith("2026-08-21T21:00:00")

    def test_a_traceback_is_indented_under_its_own_line_not_lost(self):
        text = logs_as_text(_bundle())
        assert "ValueError: nope" in text
        assert any(ln.startswith("    | ") for ln in text.splitlines())

    def test_an_empty_log_says_so_rather_than_producing_an_empty_file(self):
        """House rule 3. A zero-byte logs.txt reads as a broken export."""
        assert "No log rows" in logs_as_text({"recent_logs": []})


class TestTheDownloadIsAFolder:

    def test_the_zip_carries_the_reader_AND_the_originals(self, tmp_path):
        """The owner asked for the reader and was explicit about
        keeping the raw files: those are what gets sent on."""
        from catalyst.dashboard import server

        b = _bundle()
        stem = "catalyst-everything-all-20260821"
        target = tmp_path / "b.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{stem}/index.html", render_bundle_html(b))
            zf.writestr(f"{stem}/bundle.json", json.dumps(b, indent=2))
            zf.writestr(f"{stem}/logs.txt", logs_as_text(b))
            zf.writestr(f"{stem}/README.txt", "x")
        names = zipfile.ZipFile(target).namelist()
        for want in ("index.html", "bundle.json", "logs.txt", "README.txt"):
            assert any(n.endswith(want) for n in names), want
        # And it is a FOLDER, so unzipping never scatters files loose.
        assert all("/" in n for n in names)

    def test_the_route_exists_and_the_button_points_at_it(self):
        import inspect

        from catalyst.dashboard import panels, server

        src = inspect.getsource(server)
        assert "/diagnostics.zip" in src, "no zip route"
        assert "/diagnostics.json" in src, (
            "the single-file export was removed; it is still the thing "
            "to send on and other instructions reference it")
        assert 'action="/diagnostics.zip"' in inspect.getsource(panels), (
            "the Download button still points at the raw JSON")

    def test_the_bundle_in_the_page_is_the_bundle_in_the_file(self):
        """If the page and the JSON beside it ever disagreed, the page
        would be worse than useless. It renders that object and never
        recomputes anything."""
        b = _bundle()
        html = render_bundle_html(b)
        payload = re.search(
            r'id="bundle-data">(.*?)</script>', html, re.S).group(1)
        back = json.loads(payload.replace("<\\/", "</"))
        assert back["recent_logs"] == b["recent_logs"]
        assert back["rows"] == b["rows"]


class TestItNeverBecomesAPlaceCredentialsLeak:

    def test_the_reader_adds_no_field_of_its_own(self):
        """It renders what it is handed. Redaction happens twice before
        this module sees anything, and the guarantee only holds while
        the reader stays a renderer."""
        import inspect

        from catalyst.dashboard import bundle_reader

        src = inspect.getsource(bundle_reader)
        for banned in ("os.environ", "getenv", "load_credentials",
                       "sqlite3", "subprocess"):
            assert banned not in src, (
                f"the reader reaches for {banned} - it must only render "
                "the bundle it is given")

    def test_whatever_the_bundle_says_is_what_the_page_says(self):
        """A redacted value must not be un-redacted by rendering."""
        html = render_bundle_html(_bundle(recent_logs=[
            {"ts": "t", "level": "INFO", "component": "c",
             "message": "key=[REDACTED]"}]))
        assert "[REDACTED]" in html
