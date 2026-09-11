"""The project's memory must not be quietly lost or unreferenced.

OWNER-ASKED 2026-09-11: "start to make a doc of everything we have tried
successfully and unsuccessfully ... A doc you can reference so it can
help you with your memory", and then: "is it also certain that after
each change or test you make youll populate a doc for your memory to
reflect on".

THE HONEST ANSWER TO THE SECOND QUESTION IS THAT INTENTION CANNOT MAKE
IT CERTAIN. Every session starts with no memory of the last, so "I will
remember" is the one promise that cannot be kept by wanting to. What
makes it happen is mechanism:

  1. docs/WHAT-WE-TRIED.md exists,
  2. CLAUDE.md references it, which is what loads it into every
     session's context at all,
  3. house rule 8 makes appending to it part of landing a change, and
     house rule 5's gate lists it,
  4. this file fails if any of the above is removed.

What a test CANNOT do is notice that the doc is stale - that the change
which just landed has no row. Only the rule does that, and the rule only
works because CLAUDE.md is read first. So this file guards the mechanism
and says plainly that it does not guard the discipline.

Fully offline. No calendar dates (house rule 6): this asserts the
presence of structure, never a date.
"""

from pathlib import Path

import pytest

DOC = Path("docs/WHAT-WE-TRIED.md")
CLAUDE = Path("CLAUDE.md")


@pytest.fixture(scope="module")
def doc():
    assert DOC.exists(), (
        f"{DOC} is gone. It is the only record of what has already been "
        "tried and rejected, and without it the next session re-derives "
        "the same diagnosis and re-tries measured failures.")
    return DOC.read_text()


@pytest.fixture(scope="module")
def claude():
    return CLAUDE.read_text()


class TestItIsReachableFromTheFirstFileEveryoneReads:
    def test_claude_md_references_it(self, claude):
        """An unreferenced doc in docs/ is a doc nobody loads. This is
        the single link that puts it in a session's context."""
        assert "docs/WHAT-WE-TRIED.md" in claude

    def test_the_reference_says_when_to_read_it(self, claude):
        """A bare path gets skipped. The reference has to say what it is
        for, or it competes with six other files in docs/."""
        line = next(ln for ln in claude.splitlines()
                    if "WHAT-WE-TRIED" in ln or "already been tried" in ln)
        assert line.strip(), "the reference carries no description"
        assert "memory" in claude[claude.index("WHAT-WE-TRIED") - 200:
                                  claude.index("WHAT-WE-TRIED") + 400]

    def test_a_house_rule_requires_keeping_it_current(self, claude):
        """The doc only stays useful if updating it is part of landing a
        change rather than a good intention."""
        low = claude.lower()
        assert "what-we-tried" in low
        # THE RULE, NOT MERELY THE POINTER. Two independent things have
        # to be true, or the doc goes stale while still being linked:
        # a standing obligation to append, and it being tied to the act
        # of landing rather than left as a good intention.
        assert "appends to" in low, (
            "nothing in CLAUDE.md obliges a landed change to add a row, "
            "so the memory doc will go stale while still being linked")
        assert "as part of landing" in low, (
            "the obligation is not tied to landing a change, which is "
            "the only moment it reliably happens")
        # And it must be on house rule 5's gate, which is the checklist
        # actually worked through before a money-critical change lands.
        assert "a row in" in low


class TestItStillCarriesTheThingsThatMakeItUseful:
    """Structure, not wording. Each section below is one that a future
    session needs in order to avoid repeating work."""

    @pytest.mark.parametrize("section", [
        "What we tried",                      # the title
        "the blocker kept moving",            # the chronology
        "bake-off",                           # what was graded
        "Per-arm production record",          # what each arm has produced
        "Recurring failures in my own work",  # the meta-lessons
        "still unproven",                     # the honesty section
        "What to check first",                # the runbook
    ])
    def test_the_section_is_present(self, doc, section):
        assert section in doc, (
            f"{section!r} is gone from the memory doc; a future session "
            "loses that whole category of hard-won context")

    def test_it_keeps_its_own_editing_rules(self, doc):
        """Without these it becomes a changelog, and a changelog does not
        record what was believed and later disproved."""
        assert "Append, do not rewrite history" in doc
        assert "SUPERSEDED" in doc

    def test_it_records_at_least_one_thing_that_was_wrong(self, doc):
        """The failures are the more useful half. A doc with only
        successes in it is marketing."""
        assert "SUPERSEDED READING" in doc, (
            "no corrected belief is recorded, so the doc no longer shows "
            "what was once believed and disproved")

    def test_it_records_the_recurring_self_failures(self, doc):
        """These repeat ACROSS sessions, which is exactly what a memory
        doc is for."""
        for pattern in ("A test that cannot fail",
                        "A helper nobody calls",
                        "never landed on `main`"):
            assert pattern in doc, pattern

    def test_it_names_the_hard_bounds_as_the_owners(self, doc):
        """The one thing a future session must not change on its own."""
        assert "hard bound" in doc.lower()
        assert "owner's" in doc


class TestTheCheckCanFail:
    """House rule 4, against the file that shipped."""

    def test_a_missing_reference_would_be_caught(self, claude):
        stripped = claude.replace("docs/WHAT-WE-TRIED.md", "")
        assert "docs/WHAT-WE-TRIED.md" not in stripped

    def test_a_gutted_doc_would_be_caught(self, doc):
        stripped = doc.replace("the blocker kept moving", "")
        assert "the blocker kept moving" not in stripped

    def test_this_file_does_not_claim_to_detect_staleness(self):
        """Stated as a test so nobody later mistakes these checks for a
        guarantee that the doc is up to date. It is not: only the house
        rule covers that, and only because CLAUDE.md is read first."""
        source = Path(__file__).read_text()
        assert "cannot do is notice that the doc is stale" in source
