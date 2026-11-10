"""``research watcher status`` and ``research workspace`` (``sleep``, ``wake``,
``drain``, ``recover``).

``status``
prints the last heartbeat and the has-work signal from the workspace store for a person or
``workspace.doctor``. ``workspace sleep`` and ``workspace wake`` are how the host starts and
ends a sleep cycle (``watcher/sleep.py``; ``docs/hosting.md`` "Gateway, sleep and drain");
``workspace drain`` is the raw drain step on its own. Local owners acknowledge a stopped,
unmanaged gateway with ``workspace recover --runtime-stopped``. Every result is one JSON line.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from research_core import layout
from research_core.store import admissions, coworlds, settings
from research_core.store._common import parse_iso
from research_core.store.db import Store
from research_core.watcher import context, drain
from research_core.watcher.tick import PAUSED_DRAINED_KEY

NO_HERMES_HOME = (
    "HERMES_HOME is not set (the Orchestrator profile home; the workspace commands stop and"
    " poll runs through its runs API)"
)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    watcher = subparsers.add_parser("watcher", help="Inspect workspace coordinator status")
    commands = watcher.add_subparsers(dest="watcher_command", metavar="<subcommand>")
    commands.required = True
    status = commands.add_parser(
        "status", help="Print the last heartbeat, the has-work signal and the pause state"
    )
    status.set_defaults(run=run_status)

    workspace = subparsers.add_parser(
        "workspace", help="Workspace operations: start and end a sleep cycle, or drain"
    )
    workspace_commands = workspace.add_subparsers(dest="workspace_command", metavar="<subcommand>")
    workspace_commands.required = True
    sleep = workspace_commands.add_parser(
        "sleep",
        help="Get ready for the host to stop the container: pause, stop every run, flush to disk",
        description=(
            "Pause admission, settle recorded runs, drain tracked processes and flush stores."
            " Exit 0 authorizes the host to stop the workspace under lifecycle v1."
        ),
    )
    sleep.add_argument(
        "--wait",
        type=float,
        default=120.0,
        help="Seconds to let the Orchestrator's chat and wake runs finish before stopping them",
    )
    sleep.set_defaults(run=run_sleep)
    wake = workspace_commands.add_parser(
        "wake",
        help="End a sleep: clear the pause sleep set; the next tick resumes the drained work",
    )
    wake.set_defaults(run=run_wake)
    drain = workspace_commands.add_parser(
        "drain",
        help="Stop running Researcher runs and background processes; they resume after the wake",
    )
    drain.set_defaults(run=run_drain)
    recover = workspace_commands.add_parser(
        "recover", help="After stopping an unmanaged local gateway, close uncertain executions"
    )
    recover.add_argument(
        "--runtime-stopped",
        action="store_true",
        required=True,
        help="Attest the local gateway and workers have stopped; inputs are never replayed",
    )
    recover.set_defaults(run=run_recover)


def run_status(args: argparse.Namespace) -> int:
    ws = layout.from_env(os.environ)
    if not ws.store_path.exists():
        print(json.dumps({"ok": False, "error": "no workspace store yet; run prepare.py"}))
        return 1
    with Store.open(ws.store_path, kind="workspace") as wstore:
        heartbeat = wstore.get_meta("heartbeat")
        age_s = None
        if heartbeat is not None:
            age_s = round((datetime.now(UTC) - parse_iso(heartbeat)).total_seconds(), 1)
        waiting_on = json.loads(
            wstore.get_meta("has_work_reasons") or "[]"
        ) + admissions.work_reasons(wstore)
        payload = {
            "ok": True,
            "heartbeat": heartbeat,
            "heartbeat_age_s": age_s,
            "has_work": bool(waiting_on),
            "waiting_on": waiting_on,
            "paused": bool(settings.get(wstore, "paused")),
            "paused_drained": wstore.get_meta(PAUSED_DRAINED_KEY) == "1",
            "sleep_started": wstore.get_meta("sleep_started"),
            "blocked_executions": admissions.problems(wstore),
            "coordinator_instance": wstore.get_meta("coordinator_instance"),
            "work": admissions.work_status(
                wstore,
                Path(os.environ["IDE_INFERENCE_LEASE_FILE"])
                if "IDE_INFERENCE_LEASE_FILE" in os.environ
                else None,
            ),
            "pending_transitions": [],
            "restart_required": admissions.restart_required(wstore),
            "runtime_recovery_count": int(wstore.get_meta("runtime_recovery_count") or "0"),
        }
        for registered in coworlds.list(wstore):
            with Store.open(
                ws.coworld(registered["coworld_id"]).store_path, kind="coworld"
            ) as store:
                payload["pending_transitions"].extend(
                    [
                        {"coworld_id": registered["coworld_id"], **dict(row)}
                        for row in store.q(
                            "SELECT id,tool,actor,owner_instance,attempts,last_error,retry_at"
                            " FROM transition_commands WHERE result IS NULL"
                        )
                    ]
                )
    print(json.dumps(payload))
    return 0


def run_drain(args: argparse.Namespace) -> int:
    if not os.environ.get("HERMES_HOME"):
        print(json.dumps({"ok": False, "error": NO_HERMES_HOME}))
        return 1
    ctx = context.build(os.environ)
    try:
        result = drain.run(ctx)
    finally:
        close_stores(ctx)
    print(json.dumps({"ok": result["quiet"] and not result["errors"], **result}))
    return 0 if result["quiet"] and not result["errors"] else 1


def run_sleep(args: argparse.Namespace) -> int:
    from research_core.watcher import sleep

    if not os.environ.get("HERMES_HOME"):
        print(json.dumps({"ok": False, "error": NO_HERMES_HOME}))
        return 1
    if args.wait < 0:
        print(json.dumps({"ok": False, "error": "--wait must be zero or more seconds"}))
        return 1
    ctx = context.build(os.environ)
    try:
        report = sleep.go_to_sleep(ctx, wait_s=args.wait)
    finally:
        close_stores(ctx)
    flushed = sleep.flush_everything(ctx.ws, os.environ, ctx.researcher_profiles)
    volatile = sleep.volatile(report["still_running"], flushed)
    payload: dict = {"ok": not volatile, "paused": True, **report, "flushed": flushed}
    payload["volatile"] = volatile
    if volatile:
        payload["error"] = "not ready to stop: see volatile"
    print(json.dumps(payload, default=str))
    return 0 if not volatile else 1


def run_wake(args: argparse.Namespace) -> int:
    from research_core.watcher import sleep

    if not os.environ.get("HERMES_HOME"):
        print(json.dumps({"ok": False, "error": NO_HERMES_HOME}))
        return 1
    result = sleep.wake_from_env(os.environ)
    print(json.dumps({"ok": True, **result}, default=str))
    return 0


def run_recover(args: argparse.Namespace) -> int:
    """A local owner's explicit stop acknowledgment; hosted recovery uses its supervisor."""
    if os.environ.get("IDE_RUNTIME_ID"):
        print(
            json.dumps({"ok": False, "error": "Managed runtimes recover through their supervisor"})
        )
        return 1
    ws = layout.from_env(os.environ)
    with Store.open(ws.store_path, kind="workspace") as store:
        with admissions.dispatch_lock(store), store.tx(context.WATCHER_ACTOR) as tx:
            rows = store.q(
                "SELECT op_id,runtime_id FROM client_admissions"
                " WHERE execution_uncertain=1 OR resolution_required=1 OR"
                " (dispatch_started=1 AND status IN ('queued','running'))"
            )
            if any(row["runtime_id"] is not None for row in rows):
                print(
                    json.dumps(
                        {"ok": False, "error": "Managed runtimes recover through their supervisor"}
                    )
                )
                return 1
            for row in rows:
                tx.execute(
                    "UPDATE client_admissions SET status='interrupted',resolution_required=0,"
                    "execution_uncertain=0 WHERE op_id=?",
                    (row["op_id"],),
                )
                tx.event(
                    "client.finished",
                    row["op_id"],
                    {
                        "status": "interrupted",
                        "local_runtime_stopped": True,
                        "replayed": False,
                    },
                )
    print(
        json.dumps({"ok": True, "interrupted": [row["op_id"] for row in rows], "replayed": False})
    )
    return 0


def close_stores(ctx: context.TickContext) -> None:
    ctx.wstore.close()
    if ctx.store is not None:
        ctx.store.close()
