"""Sleep and wake: the host's one way to start and end a sleep cycle (design Appendix H "Sleep
and drain", amendment A5.7).

``research workspace sleep`` runs :func:`go_to_sleep` and then :func:`flush_everything`; when it
exits 0 nothing runs and every write is on disk, so the host may stop the container or snapshot
the volume. In order:

1. Pause the workspace (the ``paused`` setting, cited ``workspace sleep``) so no tick starts new
   work. A pause the user set stays theirs: sleep leaves it as it is.
2. Drain: stop live Researcher runs (their Sessions resume after the wake) and SIGTERM the
   background processes Tasks wait on (``drain.run``).
3. Let the Orchestrator's own runs finish: chat and wake runs the store knows are polled for up
   to ``wait_s``; any still live then are stopped with ``stopped_by`` ``workspace:sleep``. A
   stopped wake is retried after the wake without counting as a failed attempt; a stopped chat
   turn simply ends. Hermes cannot list runs, so a chat run started outside the seam (not
   recorded on its Session) is invisible here: the bridge finishes or stops its own runs first.
4. Wait for the signalled processes to exit, record ``meta.sleep_started`` and emit
   ``workspace.sleep``.
5. Flush (:func:`flush_everything`, after every store connection is closed): checkpoint each
   database (the workspace store, every Coworld store, the Hermes ``state.db`` of the
   Orchestrator and of each Researcher profile) into its main file, fsync it, retry a database a
   reader kept busy, then ``os.sync()`` for every other file on the volume (documents, git
   objects, logs).

Whatever is still running or not flushed is listed in ``volatile`` and the command exits 1.

``research workspace wake`` runs :func:`wake_up`: it clears the pause only when sleep set it,
clears ``meta.sleep_started`` and, when the pause ended, emits ``workspace.resumed``, whose
handler resumes the drained Researcher Sessions at the next tick. It starts no run itself.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from research_core import layout
from research_core.store import admissions, sessions, settings
from research_core.store import cli as store_cli
from research_core.store._common import now_iso
from research_core.store.db import Store
from research_core.watcher import dispatch, drain
from research_core.watcher.hermes_api import RunsError
from research_core.watcher.resume import emit_event
from research_core.watcher.tick import PAUSED_DRAINED_KEY

if TYPE_CHECKING:  # pragma: no cover
    from research_core.watcher.context import TickContext

SLEEP_CITES = "workspace sleep"
WAKE_CITES = "workspace wake"
SLEEP_STARTED_KEY = "sleep_started"
ORCHESTRATOR_WAIT_S = 120.0
STOP_WAIT_S = 30.0
POLL_S = 1.0
FLUSH_ATTEMPTS = 3


# --- sleep ------------------------------------------------------------------------------------


def go_to_sleep(
    ctx: TickContext,
    *,
    wait_s: float = ORCHESTRATOR_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Steps 1 to 4 of the module docstring; the caller closes the stores and then flushes."""
    paused_by_sleep = pause(ctx)
    drained = drain.run(ctx, sleep=sleep)
    if not drained["errors"]:
        ctx.set_meta(PAUSED_DRAINED_KEY, "1")  # the paused tick need not drain again
    orchestrator = settle_orchestrator_runs(ctx, wait_s=wait_s, sleep=sleep, clock=clock)
    processes = wait_for_processes(drained["processes"], sleep=sleep, clock=clock)
    with admissions.dispatch_lock(ctx.wstore):
        running = still_running(ctx) + [f"process:{pid}:running" for pid in processes]
        running.extend(
            f"admission:{row['op_id']}:reconciling"
            for row in ctx.wstore.q(
                "SELECT op_id FROM client_admissions"
                " WHERE execution_uncertain=1 OR"
                " (dispatch_started=1 AND run_id IS NULL AND status='queued')"
            )
        )
    started = now_iso()
    ctx.set_meta(SLEEP_STARTED_KEY, started)
    emit_event(
        ctx.wstore,
        ctx.actor,
        "workspace.sleep",
        "workspace",
        {
            "started": started,
            "paused_by_sleep": paused_by_sleep,
            "drained": drained["stopped"],
            "orchestrator_stopped": orchestrator["stopped"],
            "still_running": running,
        },
    )
    return {
        "sleep_started": started,
        "paused_by_sleep": paused_by_sleep,
        "drained": drained,
        "orchestrator_runs": orchestrator,
        "still_running": running,
    }


def pause(ctx: TickContext) -> bool:
    """Pause the workspace unless it is paused already; whether the pause is sleep's own."""
    with admissions.dispatch_lock(ctx.wstore):
        if settings.get(ctx.wstore, "paused"):
            return paused_by_sleep(ctx.wstore)
        settings.update(ctx.wstore, ctx.actor, "paused", True, cites=SLEEP_CITES)
        return True


def paused_by_sleep(wstore: Store) -> bool:
    """Whether the current pause is one ``research workspace sleep`` set (not the user's)."""
    row = wstore.one("SELECT value, cites FROM settings WHERE key = 'paused'")
    return row is not None and row["value"] == "true" and row["cites"] == SLEEP_CITES


def live_orchestrator_sessions(ctx: TickContext) -> list[dict[str, Any]]:
    """Chat and wake Sessions whose recorded run the gateway still reports live."""
    rows = ctx.wstore.q(
        "SELECT id FROM sessions WHERE kind IN ('chat', 'wake') AND run_id IS NOT NULL"
        " AND ended IS NULL ORDER BY started, id"
    )
    live = []
    for row in rows:
        session = sessions.read(ctx.wstore, row["id"])
        if drain.still_running(ctx, session):
            live.append(session)
    return live


def settle_orchestrator_runs(
    ctx: TickContext,
    *,
    wait_s: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> dict[str, Any]:
    """Wait up to ``wait_s`` for the Orchestrator's live runs to end, then stop the rest."""
    started = clock()
    pending = [session["id"] for session in live_orchestrator_sessions(ctx)]
    ended: list[str] = []
    while pending and clock() - started < wait_s:
        sleep(POLL_S)
        live = [sid for sid in pending if drain.still_running(ctx, sessions.read(ctx.wstore, sid))]
        ended.extend(sid for sid in pending if sid not in live)
        pending = live
    stopped: list[str] = []
    errors: list[dict[str, str]] = []
    for session_id in pending:
        session = sessions.read(ctx.wstore, session_id)
        sessions.set_stopped_by(ctx.wstore, ctx.actor, session_id, sessions.SLEEP_STOP)
        try:
            ctx.hermes.stop(session["run_id"], profile=session["profile"])
        except RunsError as error:
            sessions.set_stopped_by(ctx.wstore, ctx.actor, session_id, None)
            errors.append({"session": session_id, "error": error.detail})
            continue
        stopped.append(session_id)
    quiet = drain.wait_quiet(ctx, stopped, wait_s=STOP_WAIT_S, sleep=sleep)
    return {
        "waited_s": round(clock() - started, 1),
        "ended": ended,
        "stopped": stopped,
        "quiet": quiet,
        "errors": errors,
    }


def wait_for_processes(
    pids: list[int],
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    wait_s: float = STOP_WAIT_S,
) -> list[int]:
    """Wait for the signalled processes to exit; the pids still alive at the deadline."""
    started = clock()
    alive = [pid for pid in pids if process_alive(pid)]
    while alive and clock() - started < wait_s:
        sleep(POLL_S)
        alive = [pid for pid in alive if process_alive(pid)]
    return alive


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, owned by someone else
    return not _reaped_zombie(pid)


def _reaped_zombie(pid: int) -> bool:
    """A child of this process that exited is a zombie until reaped; reap it and say so."""
    try:
        done, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False  # not our child: ``kill(pid, 0)`` already said it is alive
    return done == pid


def still_running(ctx: TickContext) -> list[str]:
    """Runs the gateway still reports live: Researcher Sessions, then Orchestrator Sessions."""
    running = [f"session:{sid}:running" for sid in dispatch.live_task_sessions(ctx)]
    running.extend(f"session:{s['id']}:running" for s in live_orchestrator_sessions(ctx))
    return running


# --- flush ------------------------------------------------------------------------------------


def sleep_databases(
    ws: layout.Workspace, env: Mapping[str, str], researcher_profiles: tuple[str, ...]
) -> list[Path]:
    """The databases ``backup`` covers, plus each Researcher profile's Hermes ``state.db``."""
    databases = store_cli.lock_order(ws, env)
    if env.get("HERMES_HOME"):
        profiles_root = Path(env["HERMES_HOME"]).expanduser().parent
        for profile in researcher_profiles:
            state = profiles_root / profile / store_cli.HERMES_STATE_DB
            if state.exists() and state not in databases:
                databases.append(state)
    return databases


def flush_everything(
    ws: layout.Workspace,
    env: Mapping[str, str],
    researcher_profiles: tuple[str, ...],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Step 5 of the module docstring. Every store connection of this process must be closed."""
    pending = sleep_databases(ws, env, researcher_profiles)
    databases: dict[str, dict[str, Any]] = {}
    for attempt in range(FLUSH_ATTEMPTS):
        if attempt:
            sleep(POLL_S)
        reports = store_cli.flush_databases(pending)
        for path, report in reports.items():
            databases[path] = {**report, "fsynced": True}
        pending = [path for path in pending if not is_flushed(reports[str(path)])]
        if not pending:
            break
    os.sync()
    return {"databases": databases, "synced": True}


def is_flushed(report: Mapping[str, Any]) -> bool:
    return not report["busy"] and report["wal_bytes"] == 0


def volatile(running: list[str], flushed: Mapping[str, Any]) -> list[str]:
    """What is not yet safe to stop: running work, databases a checkpoint could not empty."""
    items = list(running)
    if "error" in flushed:
        items.append(f"flush:failed:{flushed['error']}")
        return items
    items.extend(
        f"database:{path}:not-checkpointed"
        for path, report in flushed["databases"].items()
        if not is_flushed(report)
    )
    return items


# --- wake -------------------------------------------------------------------------------------


def wake_from_env(env: Mapping[str, str]) -> dict[str, Any]:
    from research_core.watcher.context import build

    ctx = build(env)
    try:
        return wake_up(ctx)
    finally:
        ctx.wstore.close()
        if ctx.store is not None:
            ctx.store.close()


def wake_up(ctx: TickContext) -> dict[str, Any]:
    """End the sleep: clear sleep's own pause; the next tick resumes the drained Sessions."""
    slept_since = ctx.meta(SLEEP_STARTED_KEY)
    unpaused = paused_by_sleep(ctx.wstore)
    if unpaused:
        settings.update(ctx.wstore, ctx.actor, "paused", False, cites=WAKE_CITES)
    ctx.wstore.delete_meta(SLEEP_STARTED_KEY)
    to_resume = (
        [session["id"] for session in dispatch.drained_sessions(ctx)]
        if ctx.coworld is not None
        else []
    )
    if unpaused:
        emit_event(
            ctx.wstore,
            ctx.actor,
            "workspace.resumed",
            "workspace",
            {"slept_since": slept_since, "drained_sessions": to_resume},
        )
    return {
        "paused": bool(settings.get(ctx.wstore, "paused")),
        "unpaused": unpaused,
        "slept_since": slept_since,
        "drained_sessions_to_resume": to_resume,
    }
