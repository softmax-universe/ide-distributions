"""Create the conversation's current view. Only its receipt goes into the conversation."""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

from research_core.locks import file_lock
from research_core.store import sessions
from research_core.tools import _args as a
from research_core.tools.base import ToolCall, ToolError, ToolSpec, tool

DEFAULT_VIEW_MODEL = "deepseek/deepseek-v4.1-flash-1017"


def view_path(state: Path, session_id: str) -> Path:
    key = hashlib.sha256(session_id.encode()).hexdigest()
    return state / "conversation-views" / f"{key}.json"


def current_view(path: Path) -> dict | None:
    return json.loads(path.read_bytes()) if path.exists() else None


def request_catalog(call: ToolCall, session_id: str) -> str:
    """Ask the workspace backend for the renderer attached to this tool call."""
    call_id = call.ctx.env.get("IDE_TOOL_CALL_ID")
    address = call.ctx.env.get("IDE_REASONING_RELAY")
    if not call_id or not address:
        raise ToolError("component discovery needs an attached Play turn")
    host, port = address.rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=15) as connection:
        message = {"kind": "component_catalog", "session": session_id, "call": call_id}
        connection.sendall(json.dumps(message).encode() + b"\n")
        with connection.makefile("rb") as stream:
            result = json.loads(stream.readline(1024 * 1024))
    if "error" in result:
        raise ToolError(result["error"])
    return result["guide"]


def handle(call: ToolCall) -> dict:
    from openai import OpenAI

    ctx = call.ctx
    session_id = ctx.hermes_session_id()
    session = sessions.read(ctx.wstore, session_id)
    if not session or session["kind"] != "chat":
        raise ToolError("display_view requires an active chat session")
    catalog = request_catalog(call, session_id)
    with OpenAI(
        api_key=ctx.env["OPENROUTER_API_KEY"],
        base_url="https://openrouter.ai/api/v1",
        timeout=60,
        max_retries=0,
    ) as client:
        response = client.chat.completions.create(
            model=ctx.env.get("AIDE_VIEW_MODEL", DEFAULT_VIEW_MODEL),
            messages=[
                {
                    "role": "system",
                    "content": "Return one JSON view document using this catalog. "
                    "Use only the supplied facts.\n" + catalog,
                },
                {"role": "user", "content": call.get("brief")},
            ],
            response_format={"type": "json_object"},
            max_tokens=8000,
        )
    choice = response.choices[0]
    if choice.finish_reason != "stop" or not choice.message.content:
        raise ToolError("the view model returned an incomplete document")
    view = json.loads(choice.message.content)
    if (
        not isinstance(view, dict)
        or view.get("version") != 1
        or not isinstance(view.get("body"), list)
    ):
        raise ToolError("the view model returned an invalid component document")
    path = view_path(ctx.workspace.state, session_id)
    with file_lock(path.with_suffix(".lock")):
        previous = current_view(path)
        revision = previous["revision"] + 1 if previous else 1
        payload = {
            "kind": "display_view",
            "session_id": session_id,
            "revision": revision,
            "view": view,
            "call_id": ctx.env.get("IDE_TOOL_CALL_ID"),
        }
        path.with_suffix(".part").write_text(json.dumps(payload))
        path.with_suffix(".part").replace(path)
    return {"status": "prepared", "revision": revision}


SPEC = tool(
    ToolSpec(
        name="display_view",
        description=(
            "Display a component in the conversation's Views tab when it would help explain "
            "your answer, even if the user did not ask for a visual. You can also reply in chat."
        ),
        handler=handle,
        roles=frozenset({"orchestrator"}),
        executor="workspace",
        params={
            "brief": a.string("What the view should show, with the verified facts to display."),
        },
        required=("brief",),
    )
)


def page_interaction(call: ToolCall) -> dict:
    action = call.get("action")
    if action != "remember" and not call.get("ref"):
        raise ToolError("this interaction needs a ref from the current page")
    # The backend forwards this tool's completed call to the frontend that sent the turn.
    return {"status": "requested"}


PAGE_INTERACTION = tool(
    ToolSpec(
        name="page_interaction",
        description="Request a page interaction. The page handles confirmation.",
        handler=page_interaction,
        roles=frozenset({"orchestrator"}),
        executor="inproc",
        params={
            "action": a.string(
                "Which interaction to request", enum=["point", "open", "press", "go", "remember"]
            ),
            "ref": a.string("Current page outline ref for a page action"),
            "note": a.string("Short explanation or the lesson to remember"),
        },
        required=("action", "note"),
    )
)
