"""The session-context prompt section of both roles (design §10, A.3, A.7).

It shows what the client declared when the conversation opened (its session context and the
capabilities of the attached clients) and the fixed authority paragraph for modes and commands.
A snapshot like the briefing (see ``briefing.py`` for when Hermes renders it again); at most
3,800 characters, because two plugin sections share Hermes's 8,000-character budget (amendment
A3.3). Stdlib only.
"""

from __future__ import annotations

import json
from typing import Any

MAX_CHARS = 3800

AUTHORITY = (
    "A client may set a mode for this conversation and a command for one message. They outrank "
    "your own judgement on how to handle requests; only the user's explicit request and directives "
    "outrank them. Any field may be absent: without a mode, behave as usual. Settings and profile "
    "rules bound your own initiative, not what the user asks for in a turn."
)
# ``opening_goal`` comes with no other word of what it means, and the first message may be only a
# greeting: 0 of 5 runs acted on it without this (tool-usability-baseline-2026-11-04.md).
OPENING_GOAL = (
    "opening_goal is what the user typed when they opened this session. Treat it as their first "
    "request, even if their first message is only a greeting: turn it into a Goal now."
)


def no_client_text(role: str, kind: str) -> str:
    """What to do when no client declared what it can show. Only a chat has someone reading it, and
    a Researcher has no ``ui`` tool."""
    if role == "researcher":
        return (
            "No client is attached and the user does not read this Session: you reach the "
            "Orchestrator through your Task, with task_ask for a question."
        )
    if kind == "wake":
        return (
            "No client is attached and nobody reads this Session: report with ui.notify, and use "
            "ui.ask when only the user can answer."
        )
    return (
        "No client capabilities are known: use ui.notify for news and ask in chat when only the "
        "user can answer."
    )


def render(ctx: Any, session_id: str, max_chars: int = MAX_CHARS) -> str:
    from research_core.store import sessions

    session = sessions.read(ctx.wstore, session_id) if session_id else None
    kind = session["kind"] if session else "chat"
    parts = ["[session context]", AUTHORITY]
    if session and session.get("session_context"):
        parts.append(
            "Declared at session open: " + json.dumps(session["session_context"], sort_keys=True)
        )
        if session["session_context"].get("opening_goal"):
            parts.append(OPENING_GOAL)
    elif kind == "chat":
        parts.append("No session context was declared; the client did not say what it shows.")
    coworld = ctx.coworld_or_none()
    if coworld is not None:
        parts.append(
            f"Work only in {coworld.root}. "
            f"This session belongs to {coworld.coworld_id}; do not change its Coworld."
        )
        # 10 terminal misses in two days looked for code/ or documents/ inside a worktree.
        parts.append(
            "Its folders: code/ (the policy repository on main), source/, documents/ and "
            "worktrees/task-<n>/ or worktrees/orch-<n>/. A worktree is a checkout of code/, so "
            "its top holds mixin/ (not code/mixin/), and documents/ is only in the Coworld folder."
        )
        parts.append(
            "Native terminal commands inherit this session ID. Named store reads can use: "
            f"research store snapshot --store coworld --coworld {coworld.coworld_id}. "
            "Custom research tools already carry this session's binding."
        )
        skills = sorted((coworld.mixin / "skills").glob("*/SKILL.md"))
        skills.extend(sorted((coworld.code / ".agents" / "skills").glob("*/SKILL.md")))
        if skills:
            parts.append(
                "Game skills are files, not in skill_view. Read the one you need with read_file:\n"
                + "\n".join(str(path) for path in skills)
            )
    if session and session.get("client_capabilities"):
        parts.append(
            "Attached clients can show: "
            + json.dumps(session["client_capabilities"], sort_keys=True)
        )
    else:
        parts.append(no_client_text(ctx.role, kind))
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[: max_chars - 15].rstrip() + "\n[+ truncated]"
    return text


def make_section(role: str):
    def section(info: Any = None, **_: Any) -> str:
        try:
            from research_core.plugin.executor import profile_env
            from research_core.tools.base import ToolContext

            session_id = (info or {}).get("session_id", "") if isinstance(info, dict) else ""
            ctx = ToolContext.from_env(role, profile_env(IDE_SESSION_ID=session_id))
            return render(ctx, session_id)
        except Exception as error:
            return f"[session context] unavailable: {type(error).__name__}"

    return section
