"""The owner picks the research model, from a list Anthropic supplies.

Owner-asked 2026-08-23: "can we have an easy dropdown to change the
model we are using for future ref, or an easy way to call the api to get
current list of available models."

THE CONSTRAINT THAT SHAPES ALL OF THIS. Choosing a model the cost table
cannot price is not a small mistake: tracker.py records the call, fails
to price it, and the governor then blocks ALL spend until a human
intervenes - correctly, because pricing an unknown model at zero is the
TRAPS.md failure the subsystem exists to prevent. A dropdown that
offered every model Anthropic returns would let one click halt the bot.

Fully offline: every HTTP call is injected.
"""

import json

import pytest

from catalyst.research.boundary import DEFAULT_RESEARCH_MODEL
from catalyst.setup import first_run
from catalyst.setup.models import (
    AvailableModel, ModelListError, SETTING, list_models, priceable_models,
    selected_model,
)


class Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        return json.loads(self.text)


def ok(*ids):
    return lambda url, headers, params=None: Resp(
        200, {"data": [{"id": i, "display_name": i.upper()} for i in ids]})


class TestTheListComesFromAnthropic:
    def test_it_returns_what_the_api_returned(self):
        got = list_models("k", ok("claude-sonnet-5", "claude-opus-5"))
        assert {m.id for m in got} == {"claude-sonnet-5", "claude-opus-5"}

    def test_it_marks_which_ones_this_bot_can_price(self):
        got = {m.id: m.priceable
               for m in list_models("k", ok("claude-sonnet-5", "claude-brand-new"))}
        assert got["claude-sonnet-5"] is True
        assert got["claude-brand-new"] is False

    def test_priceable_models_come_first(self):
        got = list_models("k", ok("aaa-unpriceable", "claude-sonnet-5"))
        assert got[0].id == "claude-sonnet-5"

    def test_an_unpriceable_model_says_so_in_its_label(self):
        got = list_models("k", ok("claude-brand-new"))
        assert "no price for it" in got[0].label

    def test_the_key_is_sent(self):
        seen = {}

        def capture(url, headers, params=None):
            seen.update(headers)
            return Resp(200, {"data": [{"id": "claude-sonnet-5"}]})

        list_models("secret-key", capture)
        assert seen.get("x-api-key") == "secret-key"


class TestAFailedListIsExplainedNotHidden:
    """House rule 3. A dropdown that silently falls back to one entry
    looks like an API with one model in it."""

    def test_no_key_says_so(self):
        with pytest.raises(ModelListError, match="no Anthropic key"):
            list_models("", ok("claude-sonnet-5"))

    def test_an_http_error_carries_the_status_and_body(self):
        bad = lambda url, headers, params=None: Resp(401, "bad key")  # noqa: E731
        with pytest.raises(ModelListError, match="401"):
            list_models("k", bad)

    def test_an_empty_list_is_refused_not_returned(self):
        empty = lambda url, headers, params=None: Resp(200, {"data": []})  # noqa: E731
        with pytest.raises(ModelListError, match="came back empty"):
            list_models("k", empty)

    def test_an_unreachable_api_names_the_failure(self):
        def boom(url, headers, params=None):
            raise RuntimeError("dns")

        with pytest.raises(ModelListError, match="could not reach"):
            list_models("k", boom)

    def test_an_unrecognised_shape_is_refused(self):
        weird = lambda url, headers, params=None: Resp(200, {"models": []})  # noqa: E731
        with pytest.raises(ModelListError, match="shape"):
            list_models("k", weird)


class TestTheChoiceCanNeverHaltTheBot:
    """The safety property. A stored value must never reach the ledger
    as a model that cannot be priced."""

    def test_a_valid_choice_is_used(self):
        assert selected_model({SETTING: "claude-opus-5"}) == "claude-opus-5"

    def test_an_unpriceable_choice_falls_back_to_the_default(self):
        """The dangerous case: a model that WAS priceable, or was typed
        in, and that the cost table does not know. Using it would record
        an unpriced row and block all spend."""
        assert selected_model({SETTING: "claude-not-in-the-table"}) == \
            DEFAULT_RESEARCH_MODEL

    @pytest.mark.parametrize("settings", [
        None, {}, {SETTING: ""}, {SETTING: "   "}, {SETTING: None},
    ])
    def test_absent_or_blank_falls_back(self, settings):
        assert selected_model(settings) == DEFAULT_RESEARCH_MODEL

    def test_whatever_it_returns_is_always_priceable(self):
        for s in (None, {}, {SETTING: "junk"}, {SETTING: "claude-opus-5"}):
            assert selected_model(s) in priceable_models(), (
                "selected_model returned a model the ledger cannot price")


class TestTheDropdownRenders:
    def test_it_is_a_select_with_the_models_in_it(self):
        html = first_run.render_setup_page(models=[
            AvailableModel("claude-sonnet-5", "Sonnet 5", True),
            AvailableModel("claude-opus-5", "Opus 5", True)])
        assert '<select id="research_model"' in html
        assert "claude-opus-5" in html

    def test_an_unpriceable_model_is_disabled_not_hidden(self):
        """Hidden would leave the owner wondering where it went. Shown
        and unselectable, with the reason, answers the question."""
        html = first_run.render_setup_page(models=[
            AvailableModel("claude-brand-new", "Brand New", False)])
        assert "disabled" in html
        assert "no price for it" in html

    def test_the_current_model_is_preselected(self):
        html = first_run.render_setup_page(models=[
            AvailableModel("claude-sonnet-5", "Sonnet 5", True)])
        assert 'value="claude-sonnet-5" selected' in html

    def test_it_still_renders_when_the_list_could_not_be_fetched(self):
        """The page must draw when Anthropic is unreachable - and say
        why, rather than showing one option as though that were the
        answer."""
        html = first_run.render_setup_page(models=None,
                                           models_error="401 bad key")
        assert '<select id="research_model"' in html
        assert "could not be fetched" in html and "401 bad key" in html

    def test_research_model_is_saved_as_a_setting_not_a_secret(self):
        assert "research_model" in first_run._SETTING_FIELDS
        assert "research_model" not in first_run._SECRET_FIELD_NAMES


class TestItIsWiredIntoTheCycle:
    def test_run_cycle_accepts_the_chosen_model(self):
        import inspect

        from catalyst.orchestrator.cycle import run_cycle

        assert "research_model" in inspect.signature(run_cycle).parameters

    def test_the_scheduler_never_returns_an_unpriceable_model(self):
        from catalyst.orchestrator.scheduler import _selected_research_model

        class Creds:
            settings = {SETTING: "claude-nonsense"}

        assert _selected_research_model(Creds()) in priceable_models()

    def test_a_broken_credentials_object_still_yields_the_default(self):
        from catalyst.orchestrator.scheduler import _selected_research_model

        class Exploding:
            @property
            def settings(self):
                raise RuntimeError("boom")

        assert _selected_research_model(Exploding()) == DEFAULT_RESEARCH_MODEL
