"""Credential redaction, applied at capture rather than on the way out.

ARCHITECTURE/BUILD-BRIEF: credentials are "never logged, never shown
again once saved, and never included in any diagnostic output. Redact at
capture, not on the way out."

Practically that means every path that puts stored text on a page or in
a bundle calls redact() — prompts, broker raw_response payloads, log
lines, tracebacks, the diagnostic bundle. Over-redaction is the
deliberate bias: a mangled diagnostic is an inconvenience, a leaked key
is a real-money incident.

What gets removed:
  1. Anthropic keys              sk-ant-...
  2. Alpaca key ids              PK/AK + >=10 uppercase alnum
  3. env-var-shaped assignments  NAME=value for upper-snake NAME
  4. secret-ish JSON/dict values "api_key": "...", "secret": "..."
  5. Authorization headers       Bearer/Basic ...
  6. Any value currently present in this process's credential env vars,
     matched literally — the belt to (1)-(5)'s braces.
"""

import os
import re

MASK = "[REDACTED]"

_SECRET_NAME = re.compile(
    r"(key|secret|token|password|passwd|credential|cred|auth|session)", re.I
)

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 1. Anthropic API keys.
    (re.compile(r"sk-ant-\S+"), MASK),
    # 2. Alpaca key ids (PK live/paper, AK broker).
    (re.compile(r"\b[PA]K[A-Z0-9]{10,}\b"), MASK),
    # 5. Authorization headers.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 " + MASK),
]

# 3. env-var-shaped assignment: NAME=value, NAME upper-snake, len>=3.
_ENV_ASSIGN = re.compile(
    r"\b([A-Z][A-Z0-9_]{2,})\s*=\s*(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;)&]+)"
)

# 4. secret-ish key in JSON / dict-repr form.
_JSON_SECRET = re.compile(
    r"([\"']([A-Za-z0-9_\-]*(?:key|secret|token|password|passwd|credential|auth)"
    r"[A-Za-z0-9_\-]*)[\"']\s*[:=]\s*)([\"'])([^\"'\n]*)(\3)",
    re.I,
)

_ENV_PREFIXES = ("ALPACA", "APCA", "ANTHROPIC", "CATALYST_SECRET")


def _env_literal_values() -> list[str]:
    """Values of credential-shaped env vars in this process, longest
    first so a prefix never masks half of a longer secret."""
    out = []
    for name, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        if name.startswith(_ENV_PREFIXES) or _SECRET_NAME.search(name):
            out.append(value)
    return sorted(set(out), key=len, reverse=True)


def redact(text) -> str:
    """Redact one string. Non-strings are stringified first — a dict that
    slipped through as a repr must not escape redaction."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    for value in _env_literal_values():
        text = text.replace(value, MASK)

    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)

    text = _JSON_SECRET.sub(lambda m: f"{m.group(1)}{m.group(3)}{MASK}{m.group(5)}", text)
    text = _ENV_ASSIGN.sub(lambda m: f"{m.group(1)}={MASK}", text)
    return text


def redact_obj(obj):
    """Recursive redaction for JSON-ish structures, keeping the shape so
    a diagnostic bundle is still readable."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if isinstance(key, str) and _SECRET_NAME.search(key):
                out[key] = MASK
            else:
                out[key] = redact_obj(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj
