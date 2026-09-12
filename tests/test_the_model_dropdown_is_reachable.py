"""The model dropdown is on a page the owner can actually open.

OWNER-ASKED 2026-09-12: *"where can i actually change the dropdown to
try a different model, e.g. i wanted to switch to opus 5, how easy can
i do so"*.

THE HONEST ANSWER WAS "NOWHERE", TWICE OVER, and both halves were
invisible from the code that looked correct:

1. `setup/models.list_models` - the function that asks Anthropic what
   models exist - HAD NO PRODUCTION CALLER. `SetupApp.handle` called
   `render_setup_page(self.path_prefix)` with no models argument, so the
   "live dropdown fetched from Anthropic" rendered exactly one
   hard-coded option. Its own tests passed, because they called the
   function directly. That is the third time a helper nobody calls has
   passed its tests in this project (docs/WHAT-WE-TRIED.md section 6),
   which is why the assertions here are about the CALL SITE.

2. The dropdown only ever existed on the FIRST-RUN form. Once
   configured, `/setup` renders `render_configured_page`, which offered
   the budget and the billing key and no model at all - so the only
   route to changing it was "open the full setup form" and re-pasting
   all three secrets. That is the same trap the budget was rescued from
   in an earlier round, still set for the model.

Fully offline: the model lister is injected, never a socket.
"""

import json

from catalyst.setup import first_run
from catalyst.setup.models import AvailableModel

MODELS = [
    AvailableModel("claude-sonnet-5", "Claude Sonnet 5", True),
    AvailableModel("claude-opus-5", "Claude Opus 5", True),
    AvailableModel("claude-opus-9", "Claude Opus 9", False),
]


def _app(tmp_path, lister=None, settings=None):
    from catalyst.setup import credentials as creds

    path = tmp_path / "creds.json"
    creds.save_credentials("ak", "as", "anthropic-key", "tok",
                           settings=settings or {}, path=str(path))
    return first_run.SetupApp(
        credentials_path=str(path),
        model_lister=lister or (lambda key: list(MODELS)),
        require_token=False)


def _get(app, path="/"):
    return app.handle("GET", path, headers={}).body.decode()


class TestTheListIsActuallyFetched:
    """The call site, not the function."""

    def test_the_configured_page_asks_anthropic_what_exists(self, tmp_path):
        asked = []

        def lister(key):
            asked.append(key)
            return list(MODELS)

        body = _get(_app(tmp_path, lister))
        assert asked, ("SetupApp never called the model lister, so the "
                       "dropdown cannot contain anything the API returned")
        assert "claude-opus-5" in body

    def test_the_first_run_page_asks_too(self, tmp_path):
        asked = []

        def lister(key):
            asked.append(key)
            return list(MODELS)

        body = _get(_app(tmp_path, lister), "/?replace=1")
        assert asked
        assert "claude-opus-5" in body

    def test_the_saved_key_is_what_is_used_to_ask(self, tmp_path):
        seen = []
        _get(_app(tmp_path, lambda key: seen.append(key) or list(MODELS)))
        assert seen == ["anthropic-key"]

    def test_a_model_released_after_this_code_reaches_the_dropdown(self, tmp_path):
        """The point of asking the API at all. Nothing in the repo names
        this model and it still appears, selectable."""
        future = [AvailableModel("claude-something-7", "Something 7", False)]
        body = _get(_app(tmp_path, lambda key: list(future)))
        assert 'value="claude-something-7"' in body
        assert "disabled" not in body


class TestAFailureIsExplainedNotHidden:
    """House rule 3: a zero never goes unexplained. A dropdown quietly
    showing one entry looks like an API with one model in it."""

    def test_the_reason_is_printed_beside_the_field(self, tmp_path):
        def boom(key):
            raise RuntimeError("HTTP 401 from the model list")

        body = _get(_app(tmp_path, boom))
        assert "could not be fetched" in body
        assert "401" in body

    def test_the_page_still_renders_with_the_model_in_use(self, tmp_path):
        def boom(key):
            raise RuntimeError("dns")

        body = _get(_app(tmp_path, boom,
                         settings={"research_model": "claude-opus-5"}))
        assert '<select id="research_model"' in body
        assert "claude-opus-5" in body

    def test_a_failing_lister_never_takes_the_page_down(self, tmp_path):
        class Hostile:
            def __call__(self, key):
                raise KeyboardInterrupt  # noqa: TRY002 - deliberately nasty

        app = _app(tmp_path)
        app.model_lister = lambda key: (_ for _ in ()).throw(
            ValueError("upstream shape changed"))
        resp = app.handle("GET", "/", headers={})
        assert resp.status == 200


class TestTheChoiceSurvivesASave:
    def test_the_model_in_use_is_preselected(self, tmp_path):
        body = _get(_app(tmp_path,
                         settings={"research_model": "claude-opus-5"}))
        assert 'value="claude-opus-5" selected' in body

    def test_the_model_in_use_is_offered_even_if_the_list_lacks_it(self, tmp_path):
        """Otherwise opening this page and pressing Save would switch
        the bot to whatever happened to be first in the list."""
        body = _get(_app(tmp_path, lambda key: [MODELS[0]],
                         settings={"research_model": "claude-retired-4"}))
        assert 'value="claude-retired-4" selected' in body
        assert "in use now" in body

    def test_saving_the_settings_form_stores_the_model(self, tmp_path):
        from catalyst.setup import credentials as creds
        from catalyst.setup.models import selected_model

        app = _app(tmp_path)
        resp = app.handle("POST", "/settings", body=json.dumps({
            "monthly_budget_usd": "5",
            "research_model": "claude-opus-5",
        }).encode(), headers={"content-type": "application/json"})
        assert json.loads(resp.body)["ok"] is True
        stored = creds.load_credentials(app.credentials_path).settings
        assert selected_model(stored) == "claude-opus-5"

    def test_changing_only_the_budget_does_not_reset_the_model(self, tmp_path):
        """A blank field means 'leave it alone'. Read as 'go back to the
        default' it would silently undo the owner's choice every time
        they touched the budget."""
        from catalyst.setup import credentials as creds
        from catalyst.setup.models import selected_model

        app = _app(tmp_path, settings={"research_model": "claude-opus-5"})
        app.handle("POST", "/settings", body=json.dumps({
            "monthly_budget_usd": "42",
        }).encode(), headers={"content-type": "application/json"})
        stored = creds.load_credentials(app.credentials_path).settings
        assert selected_model(stored) == "claude-opus-5"
        assert str(stored["monthly_budget_usd"]) in ("42", "42.0")

    def test_a_model_with_no_published_rate_saves_and_says_what_that_means(
            self, tmp_path):
        app = _app(tmp_path)
        resp = app.handle("POST", "/settings", body=json.dumps({
            "monthly_budget_usd": "5",
            "research_model": "claude-opus-9",
        }).encode(), headers={"content-type": "application/json"})
        payload = json.loads(resp.body)
        assert payload["ok"] is True
        assert "estimate the cost" in payload["message"]

    def test_a_pasted_sentence_is_refused_and_changes_nothing(self, tmp_path):
        from catalyst.setup import credentials as creds

        app = _app(tmp_path, settings={"research_model": "claude-opus-5"})
        resp = app.handle("POST", "/settings", body=json.dumps({
            "monthly_budget_usd": "7",
            "research_model": "<script>alert(1)</script>",
        }).encode(), headers={"content-type": "application/json"})
        payload = json.loads(resp.body)
        assert payload["ok"] is False
        assert "not a model name" in payload["message"]
        stored = creds.load_credentials(app.credentials_path).settings
        assert stored.get("research_model") == "claude-opus-5"
        # AND the budget did not move either: nothing was changed means
        # nothing was changed.
        assert str(stored.get("monthly_budget_usd")) != "7"

    def test_the_form_posts_the_field_it_renders(self, tmp_path):
        """The two halves of the save have to agree, and nothing else
        would catch a mismatch: the browser would post without the
        model, the server would read it as 'leave it alone', and the
        dropdown would appear to do nothing at all."""
        body = _get(_app(tmp_path))
        assert 'name="research_model"' in body
        assert "research_model: val('research_model')" in body
