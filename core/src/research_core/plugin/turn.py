"""The Orchestrator's chat-turn hook (design A.4, H "Notices and continuity").

Before each turn in a chat Session, Hermes's ``pre_llm_call`` hook adds to that request,
leaving the stored message as it was: the validated client context block when the message
carries one, the unread notices (background news; a wake's ``ui.tell`` arrives as a message of
its own), a short note of what changed in the store since this conversation's last turn, the
session's Coworld, and, on the first turn of a new chat after an hour away, a summary of what
happened meanwhile. Wake and task Sessions get nothing.
Stdlib only: this runs inside Hermes and must never fail a turn.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from research_core import contract
from research_core.plugin.session_context import OPENING_GOAL
from research_core.tools.catalog import Role

log = logging.getLogger(__name__)

AWAY_AFTER = timedelta(hours=1)
MAX_NOTICES = 12
MAX_CHANGED_KINDS = 10
SUMMARY_KINDS = {
    "task.completed": "Tasks completed",
    "task.failed": "Tasks failed",
    "task.merged": "Merged into main",
    "version.submitted": "Submissions",
    "version.uploaded": "Uploads",
    "task.asked": "Researcher questions",
    "task.proposed": "Proposed Tasks",
    "task.proposed_submission": "Proposed submissions",
    "goal.park": "Goals parked",
    "capability.failed": "Broken capabilities",
    "champion.changed": "Champion changes",
}


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def render_context_block(parsed: dict) -> str | None:
    """Render validated context from the durable admission, outside user message text."""
    if not parsed:
        return None
    kept, problems = contract.validate_context(parsed)
    lines = []
    if kept.get("mode"):
        mode = kept["mode"]
        lines.append(
            f"mode: {mode.get('name')}"
            + (f" — {mode['instruction']}" if mode.get("instruction") else "")
        )
    if kept.get("command"):
        command = kept["command"]
        lines.append(
            f"command (this message only): {command.get('name')}"
            + (f" — {command['instruction']}" if command.get("instruction") else "")
        )
    if kept.get("client"):
        client = kept["client"]
        bits = [client.get("kind") or "", client.get("id") or ""]
        lines.append(
            "client: "
            + " ".join(b for b in bits if b).strip()
            + (f" (guide skill: {client['guide']})" if client.get("guide") else "")
        )
    for key in ("league", "coworld", "opening_goal", "parent_session"):
        if kept.get(key):
            lines.append(f"{key}: {kept[key]}")
    if kept.get("opening_goal"):
        lines.append(OPENING_GOAL)
    if kept.get("policy"):
        lines.append(f"policy: {kept['policy']}")
    if kept.get("agent_profile"):
        lines.append(f"agent profile: {kept['agent_profile']}")
    if kept.get("view"):
        view = kept["view"]
        lines.append(
            "view: "
            + ", ".join(str(view[k]) for k in ("id", "label", "description") if view.get(k))
        )
    if kept.get("selection"):
        lines.append(f"selection: {kept['selection']}")
    if kept.get("references"):
        lines.append(f"references: {kept['references']}")
    if kept.get("on_screen"):
        lines.append(f"on screen: {', '.join(kept['on_screen'])}")
    if kept.get("replay_moment"):
        lines.append(f"replay moment: {kept['replay_moment']}")
    if kept.get("voice"):
        lines.append(f"voice: {kept['voice']}")
    if kept.get("client_data"):
        lines.append(f"client data (not interpreted): {kept['client_data']}")
    if problems:
        lines.append("ignored: " + "; ".join(problems))
    return "[client context] " + ("\n".join(lines) if lines else "empty")


def _reaction_lines(event: Any, session_id: str) -> list[str]:
    """The reactions a ``ui.patch`` event adds to this conversation (design A.5): the bridge
    writes them under ``/reactions/<message id>``, and the agent sees them at its next turn."""
    if event.payload.get("conversation") != session_id:
        return []
    lines = []
    for op in event.payload.get("ops") or []:
        path = str(op.get("path", ""))
        if op.get("op") not in ("add", "replace") or not path.startswith("/reactions/"):
            continue
        message_id = path[len("/reactions/") :].replace("~1", "/").replace("~0", "~")
        value = op.get("value")
        shown = value.get("value") if isinstance(value, dict) else value
        lines.append(f"- message {message_id}: {shown}")
    return lines


def render_turn(ctx: Any, session_id: str, *, is_first_turn: bool, now: datetime) -> str:
    """Notices, the changed-since note, the session's Coworld and the away summary, as text."""
    parts = render_turn_parts(ctx, session_id, is_first_turn=is_first_turn, now=now)
    return "\n\n".join(text for _source, text in parts)


def render_turn_parts(
    ctx: Any, session_id: str, *, is_first_turn: bool, now: datetime
) -> list[tuple[str, str]]:
    """What ``render_turn`` joins, each with the name of its source for telemetry."""
    from research_core.store import notices

    wstore = ctx.wstore
    parts: list[tuple[str, str]] = []

    last = wstore.one(
        "SELECT seq, at, payload FROM events WHERE kind = 'turn.rendered' AND subject = ?"
        " ORDER BY seq DESC LIMIT 1",
        (session_id,),
    )
    coworld = ctx.coworld_or_none()
    coworld_id = coworld.coworld_id if coworld else None
    last_any = wstore.one(
        "SELECT at FROM events WHERE kind = 'turn.rendered'"
        " AND json_extract(payload, '$.coworld_id') IS ? ORDER BY seq DESC LIMIT 1",
        (coworld_id,),
    )
    import json

    last_payload = json.loads(last["payload"]) if last else {}
    since_w = int(last_payload.get("wseq") or 0)
    since_c = int(last_payload.get("cseq") or 0)

    coworld_store = ctx.store if coworld is not None else None

    unread = notices.unread(
        wstore, limit=MAX_NOTICES, coworld_id=coworld.coworld_id if coworld else None
    )
    if unread:
        lines = [
            f"- {n['text']}" + (f" ({', '.join(n['refs'])})" if n.get("refs") else "")
            for n in unread
        ]
        parts.append(("notices", "[notices since your last turn]\n" + "\n".join(lines)))
        ids = [int(str(n["id"]).split("-")[-1]) for n in unread]
        notices.mark_read(wstore, ctx.actor, ids, by=f"session:{session_id}")

    changed: Counter[str] = Counter()
    reactions: list[str] = []
    for store, since in ((wstore, since_w), (coworld_store, since_c)):
        if store is None:
            continue
        for event in store.events_after(since, limit=500):
            if event.kind == "ui.patch":
                reactions.extend(_reaction_lines(event, session_id))
                continue
            # Workspace bookkeeping spans games. Only this Coworld's events describe its work.
            if store is wstore:
                continue
            changed[event.kind] += 1
    if last is not None and changed:
        top = ", ".join(f"{kind} ×{n}" for kind, n in changed.most_common(MAX_CHANGED_KINDS))
        parts.append(("changed_since", f"[changed since your last turn] {top}"))
    if reactions:
        shown = "\n".join(reactions[-MAX_NOTICES:])
        parts.append(("reactions", f"[reactions since your last turn]\n{shown}"))

    away_from = _parse_time(last_any["at"]) if last_any else None
    if is_first_turn and last is None and away_from is not None and now - away_from >= AWAY_AFTER:
        summary = _away_summary(wstore, coworld_store, away_from, now)
        if summary:
            parts.append(("away_summary", summary))

    if coworld is not None:
        parts.append(("session_coworld", f"[session Coworld] {coworld.coworld_id}"))
    else:
        none = "[session Coworld] none; the client must open a session for a Coworld"
        parts.append(("session_coworld", none))

    wseq, cseq = wstore.last_seq(), (coworld_store.last_seq() if coworld_store else 0)
    with wstore.tx(ctx.actor) as tx:
        tx.event(
            "turn.rendered",
            session_id,
            {"wseq": wseq, "cseq": cseq, "first": is_first_turn, "coworld_id": coworld_id},
        )
    return parts


def _away_summary(wstore: Any, coworld_store: Any, since: datetime, now: datetime) -> str:
    counts: Counter[str] = Counter()
    for store in (wstore, coworld_store):
        if store is None:
            continue
        rows = store.q(
            "SELECT kind FROM events WHERE at >= ?", (since.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),)
        )
        for row in rows:
            if row["kind"] in SUMMARY_KINDS:
                counts[row["kind"]] += 1
    if not counts:
        return ""
    hours = max(1, int((now - since).total_seconds() // 3600))
    lines = [f"- {SUMMARY_KINDS[kind]}: {n}" for kind, n in counts.most_common()]
    return (
        f"[while you were away, about {hours} h] open with a short summary of this:\n"
        + "\n".join(lines)
    )


_bound_terminals: set[tuple[str, str]] = set()


def make_pre_llm_call(
    role: Role, on_context: Callable[[str, Any, list[tuple[str, str]]], None] | None = None
) -> Callable[..., dict | None]:
    """``on_context`` hears each source added to a user message, for telemetry."""

    def pre_llm_call(
        session_id: str = "",
        task_id: str = "",
        user_message: str = "",
        is_first_turn: bool = False,
        **_: Any,
    ) -> dict | None:
        if not session_id:
            return None
        try:
            from research_core.plugin.executor import profile_env, run_in_workspace
            from research_core.store import sessions
            from research_core.tools.base import ToolContext

            env = profile_env(IDE_SESSION_ID=session_id)
            ctx = ToolContext.from_env(role, env)
            coworld = ctx.coworld_or_none()
            if task_id and coworld is not None:
                from tools.terminal_tool import register_task_env_overrides

                key = (role, task_id)
                if key not in _bound_terminals:
                    register_task_env_overrides(
                        task_id, {"cwd": str(coworld.code), "cwd_source": "session"}
                    )
                    _bound_terminals.add(key)
            if role != "orchestrator":
                return None
            session = sessions.read(ctx.wstore, session_id)
            if session is not None and session.get("kind") != "chat":
                return None
            if session is None:
                sessions.register(
                    ctx.wstore,
                    ctx.actor,
                    session_id=session_id,
                    profile=env.get("HERMES_PROFILE_NAME") or "orchestrator",
                    kind="chat",
                    client=sessions.UNKNOWN_CLIENT,
                )
            parts: list[tuple[str, str]] = []
            import json

            admission = ctx.wstore.one(
                "SELECT payload FROM client_admissions WHERE session_id = ?"
                " AND kind = 'message' AND dispatch_started = 1"
                " AND status IN ('queued', 'running') ORDER BY rowid DESC LIMIT 1",
                (session_id,),
            )
            block = render_context_block(
                json.loads(admission["payload"])["context"] if admission else {}
            )
            if block:
                parts.append(("client_context", block))
            parts.extend(
                render_turn_parts(
                    ctx, session_id, is_first_turn=is_first_turn, now=datetime.now(UTC)
                )
            )
            if session and (session["session_context"] or {}).get("league") and coworld:
                update = json.loads(
                    run_in_workspace("platform", {"action": "coworld_update"}, env, timeout_s=3)
                )
                if update.get("status") == "update_available":
                    parts.append(
                        (
                            "coworld_update",
                            "[Coworld update available] " + json.dumps(update) + "\n"
                            "Tell the player which version changed and offer to review the current "
                            "participation guide, published patch notes if available, and the old "
                            "and current Coworld manifests. Look for notes in the linked forum "
                            "or wiki and the release source. With their agreement, refresh "
                            "the local game guide and check the policy interface. Use the "
                            "session's league for practice so it runs the current version. "
                            "Decide whether a rebuild is needed before updating the mixin's "
                            "working version. Avoid repeating an offer already in this chat.",
                        )
                    )
                elif not update["ok"]:
                    parts.append(
                        (
                            "coworld_update",
                            "[Coworld version check unavailable] Use platform coworld_update to "
                            "retry before making version-sensitive changes.",
                        )
                    )
            if on_context is not None:
                on_context(session_id, user_message, parts)
            return {"context": "\n\n".join(text for _source, text in parts)}
        except Exception as error:  # never fail a turn
            log.debug("turn hook failed: %s", error)
            return None

    return pre_llm_call
