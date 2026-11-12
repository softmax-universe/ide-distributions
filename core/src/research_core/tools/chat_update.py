"""The ``chat_update`` tool: what a background wake wants the user to hear.

Nobody reads a wake's own Session, so a wake reports through this tool. An interrupting category
records an update the coordinator delivers into the user's most recent chat
(``store/chat_updates.py``). ``progress``, and any category the ``chat_updates.categories``
setting leaves out, is written as a notice at once, which the turn hook shows at the user's next
chat turn. A chat Session answers in its reply instead, so the tool refuses there.
"""

from __future__ import annotations

from research_core.store import chat_updates, sessions
from research_core.store.registry import CHAT_UPDATE_CATEGORIES, CHAT_UPDATE_PROGRESS
from research_core.tools import _args as a
from research_core.tools.base import ToolCall, ToolError, ToolSpec, tool

ORCHESTRATOR = frozenset({"orchestrator"})
IN_CHAT = (
    "you are in the chat; reply here, in your answer. chat_update is for background wakes, "
    "whose Session nobody reads"
)
NOT_A_WAKE = (
    "chat_update is for background wake Sessions and this Session is not one: answer in its "
    "conversation"
)
TO_CHAT_NEXT = (
    "recorded; it reaches the user's most recent chat in this Coworld once your updates have been "
    "quiet for 2 minutes (10 at most), or becomes a notice when there is no chat. Do not repeat it"
)
PROGRESS_NEXT = (
    "written as a notice, not a chat message: progress never interrupts the chat; the user sees "
    "it at their next chat turn. Do not repeat it"
)
DISABLED_NEXT = (
    "written as a notice, not a chat message: the chat_updates.categories setting does not include "
    "{category}, so it will not interrupt the chat. Do not repeat it"
)


def handle(call: ToolCall) -> dict:
    """Record an update for the user; only a wake may, because a chat answers itself."""
    ctx = call.ctx
    session_id = ctx.hermes_session_id()
    session = sessions.read(ctx.wstore, session_id) if session_id else None
    if session is None or session["kind"] != "wake":
        in_chat = session is not None and session["kind"] == "chat"
        raise ToolError(IN_CHAT if in_chat else NOT_A_WAKE)
    category = call.get("category")
    result = chat_updates.add(
        ctx.wstore,
        ctx.actor,
        coworld_id=session["coworld_id"],
        category=category,
        text=call.get("text"),
        refs=call.get("refs") or [],
    )
    if "update" in result:
        return {**result, "next": TO_CHAT_NEXT}
    if category == CHAT_UPDATE_PROGRESS:
        return {**result, "next": PROGRESS_NEXT}
    return {**result, "next": DISABLED_NEXT.format(category=category)}


SPEC = tool(
    ToolSpec(
        name="chat_update",
        description=(
            "From a background wake only: tell the user something, since nobody reads a wake's "
            "Session. Most categories reach their latest chat as a message soon after; progress "
            "is left as a notice they see at their next chat turn. Refused in a chat, where you "
            "reply."
        ),
        handler=handle,
        roles=ORCHESTRATOR,
        executor="inproc",
        params={
            "category": a.string(
                "Why the user should hear it: decision (only they can decide; ask it plainly), "
                "outcome (work they asked for finished), submission, failure (something broke "
                "that needs them), merge (a Version merged) or progress (routine news; never "
                "interrupts the chat).",
                enum=[*CHAT_UPDATE_CATEGORIES, CHAT_UPDATE_PROGRESS],
            ),
            "text": a.string("What the user should hear, in plain language."),
            "refs": a.strings("Records it is about, such as T-3."),
        },
        required=("category", "text"),
    )
)
