"""Which Claude model the bot researches with, and which it may use.

OWNER-ASKED 2026-08-23: "can we have an easy dropdown to change the
model we are using for future ref, or an easy way to call the api to get
current list of available models."

Both, and they are the same feature: the dropdown is populated by asking
Anthropic what exists rather than from a list in this file that would go
stale exactly when it matters.

THE CONSTRAINT THAT SHAPES THIS. Picking a model the pricing table does
not know is not a small mistake here. cost/tracker.py records the call,
fails to price it, and the governor then blocks ALL spend until a human
intervenes - correctly, because pricing an unknown model at zero is the
TRAPS.md failure the whole subsystem exists to prevent. So a dropdown
that cheerfully offered every model Anthropic returns would let one
click halt the bot.

So each model is returned with `priceable` saying whether this bot can
cost it, and the form refuses to save one it cannot. That refusal is the
feature: a model this bot cannot price is a model it cannot budget for.

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
    priceable: bool

    @property
    def label(self) -> str:
        return (self.display_name if self.priceable
                else f"{self.display_name} - this bot has no price for it")


class ModelListError(RuntimeError):
    """The list could not be fetched. Carries the real reason: a
    dropdown that silently falls back to one entry looks like an API
    with one model in it."""


def priceable_models() -> set[str]:
    """Model ids cost/pricing.py can turn into money."""
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

    known = priceable_models()
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
            priceable=mid in known))
    if not out:
        # A ZERO IS NEVER LEFT UNEXPLAINED (house rule 3).
        raise ModelListError(
            "the model list came back empty, which is not a list of no "
            f"models - raw answer: {str(getattr(resp, 'text', ''))[:300]}")
    # Priceable first: the ones that can actually be selected.
    return sorted(out, key=lambda m: (not m.priceable, m.display_name))


#: Settings key the dropdown writes.
SETTING = "research_model"


def selected_model(settings: dict | None) -> str:
    """The model the bot should research with.

    Falls back to the built-in default whenever the setting is absent,
    blank, or names something this bot cannot price - a stored value
    that has since stopped being priceable must not be able to halt the
    governor on the next start-up.
    """
    from catalyst.research.boundary import DEFAULT_RESEARCH_MODEL

    chosen = str((settings or {}).get(SETTING) or "").strip()
    if chosen and chosen in priceable_models():
        return chosen
    return DEFAULT_RESEARCH_MODEL
