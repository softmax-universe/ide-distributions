"""Read-only session/run queries, pending-message rendering and event persistence."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from research_core.store import sessions, tasks
from research_core.store.actors import Actor
from research_core.store.errors import StoreError
from research_core.watcher.hermes_api import RunStatus

if TYPE_CHECKING:  # pragma: no cover
    from research_core.store.db import Store
    from research_core.watcher.context import TickContext

MESSAGE_LIMIT = 2000  # characters of one Orchestrator message carried on a resume


def run_status(ctx: TickContext, session: dict[str, Any] | None) -> RunStatus | None:
    """The Session's run as the gateway reports it now; None without a run."""
    if not session or not session.get("run_id"):
        return None
    return ctx.hermes.status(session["run_id"], profile=session["profile"])


def is_live(status: RunStatus | None) -> bool:
    """A run that still occupies the gateway (not terminal), whatever its phase."""
    return status is not None and not status.terminal


def holder_session(ctx: TickContext, task: dict[str, Any]) -> dict[str, Any] | None:
    """The Session row of the Task's holder, or None when the Task is not held or unknown."""
    if not task.get("held_by"):
        return None
    return sessions.read(ctx.wstore, task["held_by"])


def emit_event(store: Store, actor: Actor, kind: str, subject: str, payload: dict) -> int:
    """The watcher's own events (``session.dead``, ``task.stuck``, ``recovery.failed``): one
    ``events`` row in its own transaction, no record changes."""
    with store.tx(actor) as tx:
        return tx.event(kind, subject, payload)


def _pending_message_block(ctx: TickContext, session: dict[str, Any]) -> tuple[str, list[int]]:
    """The undelivered Orchestrator messages of the Session's Task, rendered, with their ids."""
    if session.get("kind") != "task" or not session.get("task_id") or ctx.store is None:
        return "", []
    if ctx.coworld is not None and session.get("coworld_id") not in (None, ctx.coworld.coworld_id):
        return "", []
    try:
        pending = tasks.pending_messages(ctx.store, session["task_id"], "to_researcher")
    except StoreError:
        return "", []
    reserved = set()
    for row in ctx.wstore.q(
        "SELECT a.delivery_ids FROM client_admissions a JOIN sessions s ON s.id=a.session_id"
        " WHERE s.coworld_id=? AND s.task_id=? AND a.delivery_ids!='[]'"
        " AND (a.status IN ('queued','running') OR a.dispatch_started=1"
        " OR a.disposition='delivery retries exhausted'"
        " OR (a.advanced_at IS NULL AND a.cancel_requested=0"
        " AND a.status IN ('failed','interrupted','cancelled')))",
        (session["coworld_id"], session["task_id"]),
    ):
        reserved.update(json.loads(row["delivery_ids"]))
    pending = [message for message in pending if message["id"] not in reserved]
    if not pending:
        return "", []
    lines = ["Messages from the Orchestrator while you were away:"]
    for message in pending:
        lines.append(f"- [{message['kind']}] {message['text'][:MESSAGE_LIMIT]}")
    if any(message["kind"] == "answer" for message in pending):
        lines.append(
            "Act on the answer. If it identifies a remaining blocker, register it with task_wait "
            "and end. Use the specific wait kind, such as credits for a depleted balance. Use "
            "kind=user with no ref only when the user must do something, and say what in detail. "
            "Do not ask the same question again."
        )
    return "\n".join(lines), [message["id"] for message in pending]
