"""The ``ui`` tool (design A.8, A.10): what the Orchestrator shows, tells and asks the user.

Everything is a store write, never a wait on a browser: ``show`` and ``panel_set`` patch the
conversation's UI state document, ``ask`` opens a question in it, ``notify`` writes a notice,
and ``tell`` (wakes only) records an update the coordinator delivers into the user's chat
(``store/chat_updates.py``). The conversation key follows A3.5: a chat Session writes under its
own session id; a wake Session writes under its Coworld's shared conversation ``coworld:<id>``,
and one with no Coworld under ``workspace``. The ``agent-context`` panel always lives in the
Coworld's shared conversation, where the watcher keeps its computed part: a write sets only the
needs. UI state, notices and chat updates live in the workspace store.
"""

from __future__ import annotations

import json
import secrets
from importlib import resources
from pathlib import Path
from typing import Any

from research_core import refs
from research_core.store import chat_updates, notices, sessions, ui_state
from research_core.store.registry import CHAT_UPDATE_CATEGORIES
from research_core.tools import _args as a
from research_core.tools.base import ActionSpec, ToolCall, ToolContext, ToolError, ToolSpec, tool

ORCHESTRATOR = frozenset({"orchestrator"})
WORKSPACE_CONVERSATION = "workspace"
AGENT_CONTEXT_PANEL = "agent-context"
ANSWER_NOTE = "the answer arrives as a UI action; if no client can answer, ask in chat"
ANSWER_NOTE_NO_CHAT = (
    "nobody reads this Session: the answer arrives later as a UI action; end the run"
)
TELL_IN_CHAT = (
    "you are in the chat; reply here, in your answer. ui.tell is for background wakes, whose "
    "Session nobody reads"
)
TELL_NOT_A_WAKE = (
    "ui.tell is for background wake Sessions and this Session is not one: answer in its "
    "conversation, or use ui.notify"
)
TELL_NEXT = (
    "recorded; it reaches the user's most recent chat in this Coworld once your updates have been "
    "quiet for 2 minutes (10 at most), or becomes a notice when there is no chat. Do not repeat "
    "it with ui.notify"
)
TELL_AS_NOTICE = (
    "written as a notice, not a chat message: the chat_updates.categories setting does not include "
    "{category}, so it will not interrupt the chat. Do not repeat it"
)


def _panel_kinds() -> list[str]:
    """The panel kinds of the published UI-state contract (``contract/ui-state.schema.json``)."""
    schema_file = resources.files("research_core").joinpath("contract", "ui-state.schema.json")
    schema = json.loads(schema_file.read_text())
    return list(
        schema["properties"]["panels"]["additionalProperties"]["properties"]["kind"]["enum"]
    )


PANEL_KINDS = _panel_kinds()


def coworld_conversation(coworld_id: str | None) -> str:
    """The shared UI state of one Coworld; unbound workspace state contains no game work."""
    return f"coworld:{coworld_id}" if coworld_id else WORKSPACE_CONVERSATION


def conversation_for(ctx: ToolContext) -> str:
    """A3.5: a chat Session's own id; the shared conversation of its Coworld for any other."""
    session_id = ctx.hermes_session_id()
    row = sessions.read(ctx.wstore, session_id) if session_id else None
    if row is not None and row["kind"] == "chat":
        return session_id
    return coworld_conversation(row["coworld_id"] if row else None)


def handle(call: ToolCall) -> dict:
    ctx = call.ctx
    if call.action == "notify":
        coworld = ctx.coworld_or_none()
        record = notices.add(
            ctx.wstore,
            ctx.actor,
            coworld_id=coworld.coworld_id if coworld else None,
            text=call.get("text"),
            refs=call.get("refs") or [],
            urgency=call.get("urgency") or "normal",
        )
        return {"record": record}
    if call.action == "tell":
        return tell(ctx, call.get("category"), call.get("text"), call.get("refs") or [])
    conversation = conversation_for(ctx)
    at = ctx.wstore.now()
    if call.action == "show":
        value = show_value(call.get("ref"), call.get("view"), at)
        record = patch(ctx, conversation, "/show", value)
        return {"conversation": conversation, "show": value, "version": record["version"]}
    if call.action == "panel_set":
        panel, kind = call.get("panel"), call.get("kind")
        content = call.get("content")
        if panel == AGENT_CONTEXT_PANEL:
            coworld = ctx.coworld_or_none()
            conversation = coworld_conversation(coworld.coworld_id if coworld else None)
            content = agent_context_content(ctx, conversation, content)
        if kind == "file":
            content = document_path(ctx, content)
        value = {"kind": kind, "content": content, "at": at}
        record = patch(ctx, conversation, f"/panels/{pointer_token(panel)}", value, "panels")
        return {"conversation": conversation, "panel": panel, "version": record["version"]}
    if call.action == "ask":
        options = call.get("options") or []
        if not all(isinstance(option, str) and option for option in options):
            raise ToolError("options is a list of short answer texts")
        given_refs = sorted(call.get("refs") or [])
        waiting = open_ask_about(ctx, conversation, given_refs)
        if waiting is not None:
            ask_id, existing = waiting
            return {
                "conversation": conversation,
                "ask_id": ask_id,
                "ask": existing,
                "already_open": True,
                "next": (
                    "an open question about the same records is already waiting for the user;"
                    " no second one was added. Its answer arrives as a UI action"
                ),
            }
        ask_id = f"ask_{secrets.token_hex(4)}"
        value = {
            "question": call.get("question"),
            "options": options,
            "refs": call.get("refs") or [],
            "status": "open",
            "at": at,
        }
        record = patch(ctx, conversation, f"/asks/{ask_id}", value, "asks")
        return {
            "conversation": conversation,
            "ask_id": ask_id,
            "ask": value,
            "version": record["version"],
            "next": ANSWER_NOTE if conversation == ctx.hermes_session_id() else ANSWER_NOTE_NO_CHAT,
        }
    raise AssertionError(call.action)


def tell(ctx: ToolContext, category: Any, text: Any, given_refs: list[str]) -> dict:
    """Record an update for the user's chat; only a wake may, because a chat answers itself.
    A category the ``chat_updates.categories`` setting leaves out is written as a notice, and
    the result says so."""
    session_id = ctx.hermes_session_id()
    session = sessions.read(ctx.wstore, session_id) if session_id else None
    if session is None or session["kind"] != "wake":
        in_chat = session is not None and session["kind"] == "chat"
        raise ToolError(TELL_IN_CHAT if in_chat else TELL_NOT_A_WAKE)
    result = chat_updates.add(
        ctx.wstore,
        ctx.actor,
        coworld_id=session["coworld_id"],
        category=category,
        text=text,
        refs=given_refs,
    )
    if "notice" in result:
        return {**result, "next": TELL_AS_NOTICE.format(category=category)}
    return {**result, "next": TELL_NEXT}


def open_ask_about(
    ctx: ToolContext, conversation: str, given_refs: list[str]
) -> tuple[str, dict] | None:
    """An open question in a shared conversation about exactly these records, if any.

    Wakes run in parallel and each sees the same event, so without this one decision (submit
    V-2?) reached the user as several identical cards. Only shared conversations and questions
    with refs are matched: a chat Session's own questions and ref-less ones are always added.
    """
    if not given_refs or conversation == ctx.hermes_session_id():
        return None
    asks = ui_state.get(ctx.wstore, conversation)["doc"].get("asks") or {}
    for ask_id, ask in asks.items():
        if (
            isinstance(ask, dict)
            and ask.get("status") == "open"
            and sorted(ask.get("refs") or []) == given_refs
        ):
            return ask_id, ask
    return None


def agent_context_content(ctx: ToolContext, conversation: str, content: Any) -> dict[str, Any]:
    """The panel with the given needs and the watcher's computed part unchanged."""
    needs = content.get("needs") if isinstance(content, dict) else None
    if not isinstance(needs, list):
        raise ToolError(
            'the agent-context panel takes content {"needs": [...]}; the rest is computed'
        )
    panels = ui_state.get(ctx.wstore, conversation)["doc"].get("panels") or {}
    current = (panels.get(AGENT_CONTEXT_PANEL) or {}).get("content") or {}
    return {"computed": current.get("computed"), "needs": needs}


def show_value(ref: str | None, view: str | None, at: str) -> dict[str, str]:
    if bool(ref) == bool(view):
        raise ToolError("show takes ref (a record such as T-3 or a platform id) or view, not both")
    if ref:
        if refs.parse(ref).kind == "unknown":
            raise ToolError(f"{ref!r} is not a reference: give a record id, platform id or URL")
        return {"ref": ref, "at": at}
    return {"view": view, "at": at}


def document_path(ctx: ToolContext, content: Any) -> str:
    """A file panel's content: an existing file under the Coworld documents folder, absolute."""
    if not isinstance(content, str) or not content.strip():
        raise ToolError("a file panel's content is a path under the Coworld documents folder")
    documents = ctx.coworld().documents.resolve()
    given = Path(content).expanduser()
    path = (given if given.is_absolute() else documents / given).resolve()
    if not path.is_relative_to(documents):
        raise ToolError(
            f"file panels show files under the Coworld documents folder; {content} is outside it"
        )
    if not path.is_file():
        raise ToolError(f"{content} does not exist under the Coworld documents folder")
    return str(path)


def patch(
    ctx: ToolContext, conversation: str, path: str, value: Any, container: str | None = None
) -> dict:
    """Add ``value`` at ``path``, creating the top-level ``container`` (``panels``, ``asks``)
    when the document has none yet: JSON Patch ``add`` needs the parent to exist."""
    ops: list[dict[str, Any]] = []
    if container is not None and container not in ui_state.get(ctx.wstore, conversation)["doc"]:
        ops.append({"op": "add", "path": f"/{container}", "value": {}})
    ops.append({"op": "add", "path": path, "value": value})
    return ui_state.patch(ctx.wstore, ctx.actor, conversation, ops)


def pointer_token(name: str) -> str:
    """A JSON Pointer token: ``~`` and ``/`` escaped, in that order (RFC 6901)."""
    return name.replace("~", "~0").replace("/", "~1")


SPEC = tool(
    ToolSpec(
        name="ui",
        description=(
            "What the user sees in a client. Bring a record or view into view, send a notice, or "
            "ask a question the UI answers. Writes UI state; never waits on a client. Nobody "
            "reads a wake's chat: a wake uses tell for what the user should hear now and notify "
            "for routine progress."
        ),
        handler=handle,
        roles=ORCHESTRATOR,
        executor="inproc",
        actions=(
            ActionSpec(
                "show",
                "Bring a record (ref) or a client view (view) into view; give exactly one.",
                {
                    "ref": a.string("A record such as T-3, a platform id or a platform URL."),
                    "view": a.string("A client view id, such as leaderboard."),
                },
                example_fields=("ref",),
            ),
            ActionSpec(
                "panel_set",
                "Put content in a named panel; kind is declared, never assumed. A file panel "
                "shows a file under the Coworld documents folder.",
                {
                    "panel": a.string("The panel's name."),
                    "kind": a.string("The content kind.", enum=PANEL_KINDS),
                    "content": a.any_of(
                        "The content, in the kind's shape: text (a path for file), an object "
                        "(agent-context: {needs: [...]}) or a list.",
                        a.STRING,
                        a.free_object(""),
                        {"type": "array", "items": {"anyOf": [a.STRING, a.free_object("")]}},
                    ),
                },
                ("panel", "kind", "content"),
                own_tool=True,
            ),
            ActionSpec(
                "notify",
                "Tell the user something: the notice goes in text; it informs and never asks.",
                {
                    "text": a.string("The notice itself, in plain language."),
                    "refs": a.strings("Records the notice is about, such as T-3."),
                    "urgency": a.string("Default normal.", enum=list(notices.URGENCIES)),
                },
                ("text",),
            ),
            ActionSpec(
                "tell",
                "From a background wake only: say something in the user's chat. Their latest "
                "chat gets it as a message soon after; refused in a chat, where you reply.",
                {
                    "category": a.string(
                        "Why the user should hear it now: decision (only they can decide; ask "
                        "it plainly), outcome (work they asked for finished), submission, "
                        "failure (something broke that needs them) or merge (a Version merged).",
                        enum=list(CHAT_UPDATE_CATEGORIES),
                    ),
                    "text": a.string("What the user should hear now, in plain language."),
                    "refs": a.strings("Records it is about, such as T-3."),
                },
                ("category", "text"),
            ),
            ActionSpec(
                "ask",
                "Ask something only the user can answer; the first answer from any client wins.",
                {
                    "question": a.string("The question, in plain language."),
                    "options": a.strings("Short answers to offer, if any."),
                    "refs": a.strings("Records the question is about."),
                },
                ("question",),
            ),
        ),
    )
)
