"""Which Claude model the bot researches with, and which it may use.

OWNER-ASKED 2026-08-23: "can we have an easy dropdown to change the
model we are using for future ref, or an easy way to call the api to get
current list of available models."

Both, and they are the same feature: the dropdown is populated by asking
Anthropic what exists rather than from a list in this file that would go
stale exactly when it matters.

EVERY MODEL THE API LISTS IS SELECTABLE, since 2026-09-12. Owner-asked:
*"i want nothing manual, i want it to auto add the models and also where
can i actually change the dropdown to try a different model, e.g. i
wanted to switch to opus 5"*.

WHAT USED TO STOP THAT, and it was a real constraint rather than
caution. A model absent from `cost/pricing.py`'s table could not be
priced: tracker.py recorded the call, failed to cost it, and the
governor then blocked ALL spend until a human edited that file and
redeployed. So the dropdown offered every model Anthropic returns but
DISABLED the ones it could not price, and half the point of asking the
API - that new models appear on their own - was lost the moment one
appeared.

`pricing.cold_start_rates()` removed the constraint rather than the
guard. An unpriced model is now priced at twice the dearest rate this
bot knows - deliberately high, because over-pricing throttles and can
only cost opportunity while under-pricing overspends - and the first
closed day's real bill replaces the guess. So what this module reports
is no longer "can it be priced" but WHERE THE PRICE CAME FROM, which is
the honest distinction and the one the owner needs to see before
switching.

Selecting a model NEVER changes what past calls were priced at. Rates
are looked up per model on the date of the spend, so history keeps the
model and rate it was actually bought at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class AvailableModel:
    id: str
    display_name: str
    #: True when `cost/pricing.py` carries a published rate for this
    #: model. False means it is priced from the cold-start seed until
    #: the first closed day's bill measures the real figure - still
    #: selectable, but the owner should know the estimate is a guess.
    rate_published: bool

    @property
    def label(self) -> str:
        return (self.display_name if self.rate_published else
                f"{self.display_name} - cost estimated high until the "
                "first bill")


class ModelListError(RuntimeError):
    """The list could not be fetched. Carries the real reason: a
    dropdown that silently falls back to one entry looks like an API
    with one model in it."""


def published_rate_models() -> set[str]:
    """Model ids cost/pricing.py carries a published rate for.

    NOT a list of what may be selected - every model the API lists may
    be selected, because `pricing.rates_for` can now cost any of them.
    This is only what separates "priced at Anthropic's published rate"
    from "priced at a deliberately high guess until the bill arrives".
    """
    from catalyst.cost.pricing import MODEL_RATES_CENTS_PER_MTOK

    return set(MODEL_RATES_CENTS_PER_MTOK)


def list_models(api_key: str,
                http_get: Callable | None = None) -> list[AvailableModel]:
    """Ask Anthropic what models this key can use.

    Raises ModelListError with the upstream reason on any failure - the
    caller shows it beside the dropdown rather than presenting a short
    list as though it were the answer (house rule 3).
    """
    if not (api_key or "").strip():
        raise ModelListError(
            "no Anthropic key saved yet, so the model list cannot be "
            "fetched. Save the key first and this fills in.")

    if http_get is None:
        import httpx

        def http_get(url, headers, params=None):
            return httpx.get(url, headers=headers, params=params, timeout=20.0)

    try:
        resp = http_get(MODELS_URL,
                        {"x-api-key": api_key,
                         "anthropic-version": ANTHROPIC_VERSION},
                        {"limit": 100})
    except Exception as exc:  # noqa: BLE001
        raise ModelListError(
            f"could not reach the Anthropic model list ({type(exc).__name__})"
        ) from None

    status = int(getattr(resp, "status_code", 0))
    if status != 200:
        body = (getattr(resp, "text", "") or "")[:300]
        raise ModelListError(
            f"the model list request answered HTTP {status}. {body}")
    try:
        data = resp.json()
        rows = data["data"]
    except Exception:  # noqa: BLE001
        raise ModelListError(
            "the model list came back in a shape this code does not "
            f"recognise: {str(getattr(resp, 'text', ''))[:300]}") from None

    known = published_rate_models()
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid:
            continue
        out.append(AvailableModel(
            id=mid,
            display_name=str(row.get("display_name") or mid).strip() or mid,
            rate_published=mid in known))
    if not out:
        # A ZERO IS NEVER LEFT UNEXPLAINED (house rule 3).
        raise ModelListError(
            "the model list came back empty, which is not a list of no "
            f"models - raw answer: {str(getattr(resp, 'text', ''))[:300]}")
    # Models with a published rate first - not because the others cannot
    # be chosen, but because a known cost is the safer default to land
    # on when scanning the list.
    return sorted(out, key=lambda m: (not m.rate_published, m.display_name))


#: Settings key the dropdown writes.
SETTING = "research_model"


def selected_model(settings: dict | None) -> str:
    """The model the bot should research with, and review positions with.

    Falls back to the built-in default only when the setting is absent
    or blank. It used to ALSO fall back whenever the stored model was
    absent from the pricing table, which was right while such a model
    could halt the governor and is wrong now that it cannot: silently
    researching with Sonnet while the owner's own settings page showed
    Opus selected is the "correct, present, and answering no question"
    failure this project keeps rediscovering. Any model the API lists
    can be costed, so the owner's choice is simply honoured.

    A stored id that names nothing real fails at Anthropic's own API
    with a 404 and bills no tokens - loud, and in the place that knows
    the truth - rather than being silently swapped here.
    """
    from catalyst.research.boundary import DEFAULT_RESEARCH_MODEL

    chosen = str((settings or {}).get(SETTING) or "").strip()
    return chosen or DEFAULT_RESEARCH_MODEL
