"""First-run setup: the credential store and the browser form that fills it.

Owner: integration-engineer. Nobody is ever told to edit a config file
(BUILD-BRIEF.md, "Installation and setup - assume no technical knowledge").

Two rules govern everything in this package:

1. Credentials live in one file, readable only by the service user, and
   are never written to the repository, never logged, never shown again
   once saved, and never present in a diagnostic bundle.
2. Redaction happens at the point of capture, not on the way out. Every
   public function in `credentials` registers the values it was handed
   with the redactor before it does anything else with them, so an
   exception raised three frames deeper still cannot carry one out.
"""

from catalyst.setup.credentials import (  # noqa: F401
    CredentialError,
    Credentials,
    credentials_exist,
    load_credentials,
    save_credentials,
    test_alpaca,
    test_anthropic,
)

__all__ = [
    "CredentialError",
    "Credentials",
    "credentials_exist",
    "load_credentials",
    "save_credentials",
    "test_alpaca",
    "test_anthropic",
]
