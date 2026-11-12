"""``without_mandatory_tool_use``: the ``llm_request`` middleware that edits Hermes's system prompt.

Hermes gives models it names (Kimi and GLM among them) an ``Execution discipline`` section whose
``<mandatory_tool_use>`` block says to answer arithmetic and calculations only with a tool. Kimi
K3.1 Pro reads a word count as arithmetic, so asked for an essay of a given length it writes the
essay into a tool call to count it, and the user sees nothing until that call is complete. Hermes
has a setting for the whole section and none for this block, so each request drops the block.
Only the request changes: the stored prompt keeps it, and Sessions already open lose it too.
``plugin/capture.py``'s middleware is registered after this one, so it records the request, and
matches prompt sections against it, as sent without the block.
Measured on Kimi K3.1 Pro on 2026-11-06 (README, "Develop and test").

This runs inside Hermes: stdlib only, never raising.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

MANDATORY_TOOL_USE = re.compile(r"<mandatory_tool_use>.*?</mandatory_tool_use>\n*", re.DOTALL)


def drop_mandatory_tool_use(text: str) -> str:
    return MANDATORY_TOOL_USE.sub("", text)


def _without_block(content: Any) -> Any:
    """A system message's content, a string or a list of parts, without the block."""
    if isinstance(content, str):
        return drop_mandatory_tool_use(content)
    if isinstance(content, list):
        return [
            {**part, "text": drop_mandatory_tool_use(part["text"])}
            if isinstance(part, dict) and isinstance(part.get("text"), str)
            else part
            for part in content
        ]
    return content


def without_mandatory_tool_use(*, request: dict[str, Any], **_: Any) -> dict[str, Any] | None:
    """``llm_request`` middleware: send the system message without ``<mandatory_tool_use>``."""
    try:
        messages = request.get("messages") or []
        if not messages or messages[0].get("role") not in ("system", "developer"):
            return None
        content = _without_block(messages[0].get("content"))
        if content == messages[0].get("content"):
            return None
        kept = [{**messages[0], "content": content}, *messages[1:]]
        return {"request": {**request, "messages": kept}, "reason": "Drop mandatory_tool_use"}
    except Exception as error:  # never break a model call
        log.debug("mandatory_tool_use middleware failed: %s", error)
        return None
