"""The master bundle must contain the data, not a description of it.

OWNER-REPORTED: "I also want a log that is literally every and anything
so you can dissect it, i dont want you to be missing any data. I want to
give you the master log and you be able to have full overview."

The bundle labelled "Everything" contained ROW COUNTS. Verified by
running before this was written:

    row_counts: [('candidates', 2), ('orders', 1), ...]
    recent_logs: 2 entries
    ...and no candidate rows, no research calls, no orders, no fills.

A count cannot be dissected. "candidates: 2" says nothing about which
two, what was found about them, what the model concluded or why the
risk engine refused. Sending that and calling it the master log wastes a
round trip and, worse, looks complete.

So `everything` now reads whole tables. The old summary keeps its place
as `all` - renamed "Overview", because that is honestly what it is - and
the two are no longer confusable.

WHAT MUST STAY TRUE OF A FULL DUMP:

  - Redaction is unchanged. More data must never mean weaker masking;
    credentials are stripped at capture and again over the whole file.
  - Truncation is DECLARED. A bundle that quietly drops rows is worse
    than one that says it could not carry them, because the reader
    cannot tell an empty table from an omitted one.
"""

import json
import sqlite3

import pytest

from catalyst.dashboard.db import Db
from catalyst.dashboard.server import (
    DIAGNOSTIC_SCOPES,
    FULL_DUMP_ROWS_PER_TABLE,
    diagnostics_bundle,
)
from tests.test_dashboard import bare, seeded  # noqa: F401 - shared fixtures


class TestTheFullDumpCarriesTheData:
    def test_it_contains_ROWS_not_just_counts(self, seeded):
        """THE REPORT. A count cannot be dissected."""
        b = diagnostics_bundle(Db(seeded), scope="everything")
        assert b["rows"], "no rows at all - this is still a summary"
        assert b["rows"].get("candidates"), (
            "no candidate rows. 'candidates: 2' does not say which two, "
            "what was found, or what the model made of them")
        first = b["rows"]["candidates"][0]
        assert "ticker" in first and "catalyst_type" in first

    def test_it_covers_every_table_the_database_has(self, seeded):
        b = diagnostics_bundle(Db(seeded), scope="everything")
        assert set(b["rows"]) == set(b["tables_present"]), (
            "some tables were skipped: "
            f"{sorted(set(b['tables_present']) - set(b['rows']))}")

    def test_the_overview_is_still_a_summary(self, seeded):
        """The two must not be confusable. `all` is the readable report;
        `everything` is the record."""
        overview = diagnostics_bundle(Db(seeded), scope="all")
        assert "rows" not in overview
        assert overview["row_counts"], "the overview should keep its counts"
        assert DIAGNOSTIC_SCOPES["all"]["label"] == "Overview"

    def test_the_full_dump_is_the_bigger_of_the_two(self, seeded):
        small = len(json.dumps(diagnostics_bundle(Db(seeded), scope="all"),
                               default=str))
        big = len(json.dumps(diagnostics_bundle(Db(seeded), scope="everything"),
                             default=str))
        assert big > small


class TestMoreDataIsNotWeakerMasking:
    def test_a_credential_in_the_logs_is_still_redacted(self, seeded):
        """Redaction is not relaxed because the bundle is bigger. This
        is the one property that must hold whatever else changes."""
        conn = sqlite3.connect(seeded)
        conn.execute(
            "INSERT INTO logs (ts, level, component, message) VALUES "
            "('2026-08-14T00:00:00+00:00','ERROR','x',?)",
            ("anthropic key sk-ant-NOTAREALKEY000111222333 in the clear",))
        conn.commit()
        conn.close()
        blob = json.dumps(diagnostics_bundle(Db(seeded), scope="everything"),
                          default=str)
        assert "NOTAREALKEY000111222333" not in blob, (
            "the full dump leaked a credential the summary masked")
        assert "REDACTED" in blob

    def test_environment_values_are_still_never_included(self, seeded):
        b = diagnostics_bundle(Db(seeded), scope="everything")
        assert "env_var_names_only" in b
        assert not any(k.startswith("env_var_values") for k in b)


class TestTruncationIsDeclaredNotSilent:
    def test_a_table_over_the_cap_says_so(self, bare):
        """A bundle that quietly drops rows is worse than one that says
        it could not carry them: the reader cannot tell an empty table
        from an omitted one."""
        conn = sqlite3.connect(bare)
        conn.executemany(
            "INSERT INTO raw_events VALUES (?,?,?,?)",
            [(f"src{i}", f"id{i}", "2026-08-14T00:00:00+00:00", "{}")
             for i in range(FULL_DUMP_ROWS_PER_TABLE + 50)])
        conn.commit()
        conn.close()
        b = diagnostics_bundle(Db(bare), scope="everything")
        assert "raw_events" in b["rows_truncated"], (
            "a table over the cap was silently shortened")
        assert len(b["rows"]["raw_events"]) == FULL_DUMP_ROWS_PER_TABLE
        assert "on its own" in b["rows_truncated"]["raw_events"], (
            "truncation should say how to get the rest")

    def test_nothing_truncated_is_an_empty_dict_not_a_missing_key(self, seeded):
        """Absence of truncation must be positively stated, so a reader
        never has to wonder whether the key is missing or the file is."""
        b = diagnostics_bundle(Db(seeded), scope="everything")
        assert isinstance(b["rows_truncated"], dict)


class TestTheButtonIsThere:
    def test_the_full_dump_is_offered_and_marked_as_the_master(self, bare):
        from catalyst.dashboard import maintenance, panels

        report = maintenance.build_report(Db(bare), None, run_active=False)
        html_out = panels.maintenance_panel(report)
        # THE DOWNLOAD IS NOW A FOLDER, not a bare JSON file. Owner-asked:
        # "can we attach a html reader ... still include raw logs in the
        # folder". The zip carries index.html beside the untouched
        # bundle.json, so the route moved; what the button DOES has not.
        assert 'action="/diagnostics.zip"' in html_out
        assert 'value="everything"' in html_out
        assert "bundlebtn master" in html_out
        # ...and it is the FIRST one, because it is what to send when
        # you do not know which of the others applies.
        assert (html_out.index('value="everything"')
                < html_out.index('value="all"'))
        # ...and it is the one selected if the reader picks nothing.
        assert 'value="everything" checked' in html_out
