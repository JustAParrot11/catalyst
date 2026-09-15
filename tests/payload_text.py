"""The prompt as the MODEL sees it, whatever container it arrived in.

Both request paths now send the prompt as a one-block content list so it
can carry a `cache_control` marker; both used to send a bare string.

A test pinned to the CONTAINER breaks on a change that alters nothing
the model reads, and that has now happened twice — once on the research
path when section 21 added the marker, and again on the hunt path when
section 28 added it there. The first time it was fixed with a helper
local to `test_boundary.py`, so the hunt tests could not reach it and hit
the identical failure a day later.

One definition, imported by both, for the same reason the production
change imports one `cacheable_prompt_message` rather than copying the
marker: two copies drift, and the drift is invisible until a path turns
out to have been missed.
"""


def prompt_text(payload: dict, index: int = 0) -> str:
    """The text of `payload["messages"][index]`, string or block list."""
    content = payload["messages"][index]["content"]
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content
                   if isinstance(block, dict) and block.get("type") == "text")


def cache_marked(payload: dict, index: int = 0) -> bool:
    """Does that message carry the ephemeral cache marker?"""
    content = payload["messages"][index]["content"]
    if isinstance(content, str):
        return False
    return any(isinstance(b, dict) and b.get("cache_control")
               for b in content)


def message_text(payload: dict, index: int = -1) -> str:
    """Everything the model would READ in one message, whatever shape it
    is in: a bare string, `text` blocks, or the `content` of
    `tool_result` blocks.

    FOURTH INSTANCE of the trap this module opens with, and the first on
    a user turn rather than the prompt. Section 33's fix makes the repair
    and review follow-ups `tool_result` lists instead of plain strings -
    because the Messages API requires a `tool_result` after a `tool_use`
    and rejects the request outright without one - so a test asserting
    `"invalidation" in content` went from a substring check to a list
    membership check and failed on a change that alters not one word the
    model reads.

    `prompt_text` above deliberately reads only `text` blocks, which is
    right for a prompt; a follow-up turn's words can live in either, so
    this reads both.
    """
    content = payload["messages"][index]["content"]
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            out.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                out.append(inner)
            elif isinstance(inner, list):
                out.extend(str(b.get("text") or "")
                           for b in inner if isinstance(b, dict))
    return "".join(out)
