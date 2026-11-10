"""The ``softmax_api`` tool (design C.2): any Observatory route, with auth handled.

The escape hatch for routes no tool covers (the ``softmax-api-routes`` skill lists them). The
path is relative to ``/observatory``; the call runs as the Player, or as the User when the
Orchestrator asks (the Researcher is Player only). Every call is logged as a
``platform.request`` event (method, path, identity, outcome, status) in the Coworld store, or the
workspace store for a Session with no Coworld, so frequent routes can become proper tools. A write
made as the User leaves a notice (workspace store) once its outcome is known: "Acted as your User
identity: <METHOD> <path>" (A4.7), or "may have acted" when the platform gave no clear answer. The
user learns of anything done in their name. Runs in the workspace environment.
"""

from __future__ import annotations

from research_core.platform.wait import call_platform, identity_for
from research_core.store import notices
from research_core.tools import _args as a
from research_core.tools.base import INLINE_CHARS, ToolCall, ToolError, ToolSpec, tool

BOTH = frozenset({"orchestrator", "researcher"})
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
READ_METHODS = ("GET", "HEAD")


def handle(call: ToolCall) -> dict:
    ctx = call.ctx
    method = str(call.get("method")).upper()
    path = normalise_path(call.get("path"))
    identity = identity_for(ctx, call.get("identity"))
    params, body = call.get("params"), call.get("json")
    if method not in METHODS:
        raise ToolError(f"method must be one of {', '.join(METHODS)}, not {method!r}")
    route = f"{method} {path}"
    platform = ctx.platform()
    try:
        response = call_platform(
            ctx,
            route,
            lambda: platform.request(method, path, params=params, json=body, identity=identity),
            identity=identity,
        )
    except ToolError as error:
        _log(call, method, path, identity, outcome="error", status=error.context.get("status"))
        if error.context.get("retryable"):
            _notice_user_write(call, method, path, identity, "May have acted")
        raise
    _log(call, method, path, identity, outcome="ok", status=None)
    _notice_user_write(call, method, path, identity, "Acted")
    return {"method": method, "path": path, "identity": identity, "response": response}


def _notice_user_write(call: ToolCall, method: str, path: str, identity: str, verb: str) -> None:
    if identity != "user" or method in READ_METHODS:
        return
    coworld = call.ctx.coworld_or_none()
    notices.add(
        call.ctx.wstore,
        call.ctx.actor,
        coworld_id=coworld.coworld_id if coworld else None,
        text=f"{verb} as your User identity: {method} {path}",
        refs=(),
    )


def normalise_path(path: object) -> str:
    """``/v2/leagues``: a leading slash, the ``/observatory`` prefix dropped if given."""
    text = str(path or "").strip()
    if not text.startswith("/"):
        text = "/" + text
    return text.removeprefix("/observatory") or "/"


def _log(
    call: ToolCall, method: str, path: str, identity: str, *, outcome: str, status: int | None
) -> None:
    store = call.ctx.store if call.ctx.coworld_or_none() else call.ctx.wstore
    with store.tx(call.ctx.actor) as tx:
        tx.event(
            "platform.request",
            f"{method} {path}",
            {
                "method": method,
                "path": path,
                "identity": identity,
                "outcome": outcome,
                "status": status,
            },
        )


SPEC = tool(
    ToolSpec(
        name="softmax_api",
        description=(
            "Call any Softmax Observatory route with authentication handled, for routes no tool "
            "covers (see the softmax-api-routes skill). Paths are relative to /observatory. "
            "Runs as the Player unless an Orchestrator passes identity: user. Every call is logged."
        ),
        handler=handle,
        roles=BOTH,
        executor="workspace",
        params={
            "method": a.string("The HTTP method.", enum=list(METHODS)),
            "path": a.string("The route, such as /v2/leagues (relative to /observatory)."),
            "params": a.free_object("Query parameters, name to value."),
            "json": a.free_object("A JSON body for POST, PUT or PATCH."),
            "identity": a.string(
                "Default player; user notifies the user of every write.", enum=["player", "user"]
            ),
        },
        required=("method", "path"),
        orchestrator_fields=("identity",),
        inline_chars=INLINE_CHARS,
        spill_hint=(
            "Routes that page take params.limit for fewer items and params.cursor (the "
            "next_cursor in the outline) for the next page."
        ),
    )
)
