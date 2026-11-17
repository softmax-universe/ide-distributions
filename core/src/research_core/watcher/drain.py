"""Drain and the "has work" signal (design Appendix H "Sleep and drain"; A5.6, A5.7, A5.11).

``run`` is what an explicit pause (the tick's step 1, once per pause) and ``research workspace
drain`` call: every task Session whose run the gateway still reports live is marked
``stopped_by = watcher:drain`` *before* ``hermes.stop`` (so recovery never counts the stop as
a death, A5.6), its run is stopped and the Session is flagged ``drained``; background
processes a Researcher waits on (``waiting_on.kind == process``) get SIGTERM; then the step
waits up to a minute for the stopped runs to end and emits ``drain.done``. After the wake the
dispatch step resumes drained Sessions first, with their own session ids.

``has_work`` is the signal hosting reads to keep the workspace awake. It is true only for work
that is running or ready to run: a live Researcher run, a transition command the coordinator can
apply now, a background process a Task waits on (it runs on this machine, so a sleep would kill
it), or a queued wake. A Task waiting on the platform (XP, credits, a submission) does not keep
the workspace awake: the platform keeps working while it sleeps, and the first tick after the
wake reads the result. Neither does a transition command waiting out its ``retry_at`` backoff:
the coordinator applies it on the first tick after that time, awake or after the next wake. A
submission candidate is acted on by the management wake its ``submission.candidate`` event
queues, so it counts as ``wake:queued`` while that wake is pending.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from research_core.store import coworlds, sessions, tasks
from research_core.store._common import parse_iso
from research_core.watcher import dispatch, wakes
from research_core.watcher.context import _open_coworld_store
from research_core.watcher.hermes_api import RunsError
from research_core.watcher.resume import emit_event

if TYPE_CHECKING:  # pragma: no cover
    from research_core.watcher.context import TickContext

STOP_REASON = "watcher:drain"
WAIT_S = 60.0
POLL_S = 1.0
# Waits whose work runs on this machine; waits on the platform (xp, credits, submission) do not
# keep the workspace awake.
RUNNING_WAIT_KINDS = frozenset({"process"})


def run(
    ctx: TickContext, *, wait_s: float = WAIT_S, sleep: Callable[[float], None] = time.sleep
) -> dict[str, Any]:
    """Stop live Researcher runs and their background processes; return when quiet."""
    stopped: list[str] = []
    errors: list[dict[str, str]] = []
    if ctx.coworld is not None and ctx.store is None:
        errors.append(
            {"coworld": ctx.coworld.coworld_id, "error": "store schema is newer than this runtime"}
        )
    for session_id in dispatch.live_task_sessions(ctx):
        session = sessions.read(ctx.wstore, session_id)
        sessions.set_stopped_by(ctx.wstore, ctx.actor, session_id, STOP_REASON)
        try:
            ctx.hermes.stop(session["run_id"], profile=session["profile"])
        except RunsError as error:
            sessions.set_stopped_by(ctx.wstore, ctx.actor, session_id, None)
            errors.append({"session": session_id, "error": error.detail})
            continue
        stopped.append(session_id)
    if stopped:
        sessions.mark_drained(ctx.wstore, ctx.actor, stopped, True)
    processes = stop_processes(ctx)
    for registered in coworlds.list(ctx.wstore):
        coworld = ctx.ws.coworld(registered["coworld_id"])
        if ctx.coworld is not None and coworld.coworld_id == ctx.coworld.coworld_id:
            continue
        store = _open_coworld_store(coworld)
        if store is None:
            errors.append(
                {"coworld": coworld.coworld_id, "error": "store schema is newer than this runtime"}
            )
            continue
        try:
            processes.extend(stop_processes(replace(ctx, coworld=coworld, store=store)))
        finally:
            store.close()
    quiet = wait_quiet(ctx, stopped, wait_s=wait_s, sleep=sleep)
    emit_event(
        ctx.wstore,
        ctx.actor,
        "drain.done",
        "workspace",
        {"sessions": stopped, "processes": processes, "quiet": quiet, "errors": errors},
    )
    return {"stopped": stopped, "processes": processes, "quiet": quiet, "errors": errors}


def stop_processes(ctx: TickContext) -> list[int]:
    """SIGTERM every background process an in-progress Task waits on; the pids signalled."""
    if ctx.store is None:
        return []
    signalled: list[int] = []
    for task in tasks.list(ctx.store, status="in-progress"):
        waiting = task["waiting_on"] or {}
        if waiting.get("kind") != "process":
            continue
        pid = _pid(waiting.get("ref"))
        if pid is None:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
        signalled.append(pid)
    return signalled


def wait_quiet(
    ctx: TickContext, session_ids: list[str], *, wait_s: float, sleep: Callable[[float], None]
) -> bool:
    """Poll the stopped runs until every one is terminal or ``wait_s`` has passed."""
    deadline = time.monotonic() + wait_s
    while True:
        live = [
            session_id
            for session_id in session_ids
            if still_running(ctx, sessions.read(ctx.wstore, session_id))
        ]
        if not live:
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(POLL_S)


# --- has work ---------------------------------------------------------------------------------


def has_work(ctx: TickContext) -> bool:
    """Whether hosting should keep the workspace awake (see the module docstring)."""
    return bool(work_reasons(ctx))


def work_reasons(ctx: TickContext) -> list[str]:
    """Why the workspace must stay awake, one short reason per cause, in a fixed order: live
    Researcher runs (``session:<id>:running``), transition commands the coordinator can apply now
    (``transition:queued``), Tasks waiting on a background process (``task:T-n:waiting:process``)
    and queued wakes (``wake:queued``). Empty means sleep is fine. The heartbeat records the list;
    ``research watcher status`` shows it."""
    reasons = [f"session:{sid}:running" for sid in dispatch.live_task_sessions(ctx)]
    if ctx.store is not None:
        now = parse_iso(ctx.store.now())
        if any(
            row["retry_at"] is None or parse_iso(row["retry_at"]) <= now
            for row in ctx.store.q("SELECT retry_at FROM transition_commands WHERE result IS NULL")
        ):
            reasons.append("transition:queued")
        for task in tasks.list(ctx.store, status="in-progress"):
            kind = (task["waiting_on"] or {}).get("kind")
            if kind in RUNNING_WAIT_KINDS:
                reasons.append(f"task:{task['id']}:waiting:{kind}")
    if wakes.has_queued_wakes(ctx):
        reasons.append("wake:queued")
    return reasons


# --- helpers ----------------------------------------------------------------------------------


def still_running(ctx: TickContext, session: dict[str, Any] | None) -> bool:
    """Whether the gateway still reports the Session's run live (unreachable counts as no). A run
    lost in a gateway restart is not: nothing executes it, though the gateway holds it open until
    its owner resolves it (``Coordinator.status``)."""
    if session is None or not session.get("run_id"):
        return False
    try:
        status = ctx.hermes.status(session["run_id"], profile=session["profile"])
    except RunsError:
        return False  # an unreachable gateway cannot be waited on; it is not our run any more
    return not status.terminal and not status.lost


def _pid(ref: Any) -> int | None:
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return ref if ref > 1 else None
    if isinstance(ref, str) and ref.strip().isdigit():
        return int(ref.strip()) if int(ref.strip()) > 1 else None
    return None
