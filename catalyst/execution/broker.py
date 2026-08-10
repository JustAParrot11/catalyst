"""Thin Alpaca adapter. HUMAN REVIEW REQUIRED.

Credentials come from the runtime credential store written by the setup
UI (stage 7) - never from the repository, never logged. In the build
sandbox they arrive via environment variables for live paper
verification only; nothing in this module ever prints or persists them.

Paper account facts verified live 2026-08-10 (STRATEGY-PROPOSALS.md
section 1): shorting_enabled=false, multiplier=1, PDT fields absent,
corporate actions live at /v1/corporate-actions (not /v2/).
"""

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"


class Broker:
    """All Alpaca calls go through this one class so stress-tester has a
    single seam to attack and tests have a single seam to stub."""

    def __init__(self, key_id: str, secret_key: str, base_url: str = PAPER_BASE_URL):
        raise NotImplementedError("stage 5: built against the live paper account")
