"""The owner picks the research model, from a list Anthropic supplies.

Owner-asked 2026-08-23: "can we have an easy dropdown to change the
model we are using for future ref, or an easy way to call the api to get
current list of available models."

Owner-asked 2026-09-12: *"i want nothing manual, i want it to auto add
the models and also where can i actually change the dropdown to try a
different model, e.g. i wanted to switch to opus 5"*.

THE CONSTRAINT THAT USED TO SHAPE ALL OF THIS, and no longer does.
Choosing a model the cost table could not price was not a small mistake:
tracker.py recorded the call, failed to price it, and the governor then
blocked ALL spend until a human edited pricing.py and redeployed. So the
dropdown DISABLED any model without a published rate - which meant the
live list's whole purpose, that new models appear on their own, died the
moment one appeared.

`pricing.cold_start_rates()` removed the constraint rather than the
guard: an unpriced model is costed at twice the dearest known rate -
deliberately high, because over-estimating only throttles while
under-estimating overspends - and the first closed day's real bill
replaces the guess. So every model the API lists is now selectable, and
several assertions in this file are the INVERSE of what they were. They
are kept as inversions rather than deleted so the old belief stays
visible in the record.

Fully offline: every HTTP call is injected.
"""

import json

import pytest

from catalyst.research.boundary import DEFAULT_RESEARCH_MODEL
from catalyst.setup import first_run
from catalyst.setup.models import (
    AvailableModel, ModelListError, SETTING, list_models,
    published_rate_models, selected_model,
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

    def test_it_marks_which_ones_have_a_published_rate(self):
        got = {m.id: m.rate_published
               for m in list_models("k", ok("claude-sonnet-5", "claude-brand-new"))}
        assert got["claude-sonnet-5"] is True
        assert got["claude-brand-new"] is False

    def test_models_with_a_published_rate_come_first(self):
        got = list_models("k", ok("aaa-unknown-rate", "claude-sonnet-5"))
        assert got[0].id == "claude-sonnet-5"

    def test_a_model_with_no_published_rate_says_its_cost_is_a_guess(self):
        """NOT 'this bot has no price for it' any more - it has one, and
        the honest thing to say is where the number came from."""
        got = list_models("k", ok("claude-brand-new"))
        assert "estimated high until the first bill" in got[0].label

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


class TestTheChoiceIsHonoured:
    """INVERTED 2026-09-12. The old property was 'a model the table does
    not know falls back to the default', which was right while such a
    model could halt the governor. It cannot now, and silently
    researching with Sonnet while the settings page shows Opus selected
    is the failure this project keeps rediscovering: the page holds the
    fact and answers no question."""

    def test_a_model_with_a_published_rate_is_used(self):
        assert selected_model({SETTING: "claude-opus-5"}) == "claude-opus-5"

    def test_a_model_released_after_this_code_is_also_used(self):
        assert selected_model({SETTING: "claude-opus-9"}) == "claude-opus-9"

    @pytest.mark.parametrize("settings", [
        None, {}, {SETTING: ""}, {SETTING: "   "}, {SETTING: None},
    ])
    def test_absent_or_blank_falls_back(self, settings):
        assert selected_model(settings) == DEFAULT_RESEARCH_MODEL

    def test_whatever_it_returns_can_always_be_priced(self):
        """The property that MATTERS, and it survives the inversion: an
        unpriceable model would record an unpriced row and block all
        spend. Every answer must be costable, whether from the published
        table or from the cold-start seed."""
        from datetime import date

        from catalyst.cost.pricing import rates_for

        for s in (None, {}, {SETTING: "claude-opus-9"},
                  {SETTING: "claude-opus-5"}):
            inp, outp = rates_for(selected_model(s), date(2026, 9, 12))
            assert inp > 0 and outp > 0


class TestTheDropdownRenders:
    def test_it_is_a_select_with_the_models_in_it(self):
        html = first_run.render_setup_page(models=[
            AvailableModel("claude-sonnet-5", "Sonnet 5", True),
            AvailableModel("claude-opus-5", "Opus 5", True)])
        assert '<select id="research_model"' in html
        assert "claude-opus-5" in html

    def test_a_model_with_no_published_rate_is_selectable(self):
        """INVERTED. It used to be rendered `disabled`, which is what
        made the live list pointless: the only models on it that could
        be chosen were the ones already written into pricing.py."""
        html = first_run.render_setup_page(models=[
            AvailableModel("claude-brand-new", "Brand New", False)])
        assert "disabled" not in html
        assert "estimated high until the first bill" in html

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

    def test_the_scheduler_returns_the_owners_choice_verbatim(self):
        from catalyst.orchestrator.scheduler import _selected_research_model

        class Creds:
            settings = {SETTING: "claude-opus-9"}

        assert _selected_research_model(Creds()) == "claude-opus-9"

    def test_a_broken_credentials_object_still_yields_the_default(self):
        from catalyst.orchestrator.scheduler import _selected_research_model

        class Exploding:
            @property
            def settings(self):
                raise RuntimeError("boom")

        assert _selected_research_model(Exploding()) == DEFAULT_RESEARCH_MODEL

    def test_published_rate_models_is_not_a_gate_on_selection(self):
        """It reports where a price came from. Using it to decide what
        may be SELECTED is what this change removed, and re-adding that
        check anywhere would silently reintroduce the manual step."""
        assert "claude-opus-9" not in published_rate_models()
        assert selected_model({SETTING: "claude-opus-9"}) == "claude-opus-9"
