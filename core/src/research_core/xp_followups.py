"""Deliver experiment completions to the originating agent."""

from __future__ import annotations

import asyncio
import json
from itertools import batched
from pathlib import Path
from sqlite3 import Row
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from research_core.store import admissions, sessions, settings
from research_core.store.actors import Actor
from research_core.store.db import Store

if TYPE_CHECKING:
    from research_core.platform.experience_changes import ReadChanges


def register(
    store: Store, actor: Actor, session_id: str, experiment: str, requests: list[str]
) -> bool:
    session = sessions.read(store, session_id)
    if session is None or session["kind"] not in {"chat", "wake"}:
        return False
    untracked = set(requests)
    pending = False
    with store.tx(actor) as tx:
        for row in tx.q(
            "SELECT request_ids,admission_id FROM xp_followups WHERE session_id=? AND experiment=?",
            (session_id, experiment),
        ):
            tracked = set(json.loads(row["request_ids"]))
            if row["admission_id"] is None and not untracked.isdisjoint(tracked):
                pending = True
            untracked.difference_update(tracked)
        if not untracked:
            return pending
        request_ids = json.dumps(sorted(untracked))
        key = str(uuid5(NAMESPACE_URL, json.dumps([session_id, experiment, request_ids])))
        tx.execute(
            "INSERT INTO xp_followups(id,session_id,experiment,request_ids,created_at)"
            " VALUES (?,?,?,?,?)",
            (key, session_id, experiment, request_ids, tx.at),
        )
    return True


def register_task(
    store: Store, actor: Actor, task_id: int, experiment: str, request_id: str
) -> None:
    """Join the run-record transaction so an accepted request cannot lose its follow-up."""
    with store.tx(actor) as tx:
        row = tx.one(
            "SELECT request_ids FROM task_xp_waits WHERE task_id=? AND experiment=?",
            (task_id, experiment),
        )
        ids = json.loads(row["request_ids"]) if row is not None else []
        if request_id in ids:
            return
        tx.execute(
            "INSERT INTO task_xp_waits(task_id,experiment,request_ids) VALUES (?,?,?)"
            " ON CONFLICT(task_id,experiment) DO UPDATE SET request_ids=excluded.request_ids",
            (task_id, experiment, json.dumps([*ids, request_id])),
        )


async def deliver(read: ReadChanges, store_path: Path, row: Row) -> None:
    """Observe the whole batch before handing it to the durable chat queue."""
    from research_core.platform.experience_changes import MAX_ROOTS, wait_for_terminal

    async with asyncio.TaskGroup() as watches:
        batches = [
            watches.create_task(wait_for_terminal(read, ids))
            for ids in batched(json.loads(row["request_ids"]), MAX_ROOTS)
        ]
    reason = (
        "have reached terminal states"
        if all(batch.result() for batch in batches)
        else "finished or are no longer accessible on the platform"
    )
    audience = (
        "Report the outcome in this chat."
        if row["kind"] == "chat"
        else "Tell the user with chat_update if the outcome warrants it."
    )

    with Store.open(store_path, kind="workspace") as store, store.tx(Actor.harness()) as tx:
        pending = tx.one("SELECT admission_id FROM xp_followups WHERE id=?", (row["id"],))
        if pending["admission_id"] is not None or settings.get(store, "paused"):
            return
        experiment = row["experiment"]
        op_id = admissions.enqueue_in_transaction(
            tx,
            key=f"xp-followup:{row['id']}",
            session_id=row["session_id"],
            profile=row["profile"],
            origin="followup",
            text=(
                f"The watched experience requests for {experiment} {reason}. "
                "This is an automatic follow-up. Read the experiment and use xp.status "
                "to check the actual results, including failed or missing results. "
                "If evaluation is still running, use its resume token to finish it. "
                "Read the active directives and the experiment's Goal, if it has one. "
                "If the user asked you to keep improving and the Goal is still active, check its "
                "open Tasks and experiments. If none is advancing the next evidence-backed step, "
                "start it, assign it, or record a Task with its actual blocker. Stay within the "
                "user's scope and ask only for decisions they reserved or only they can make. "
                f"{audience} Say what actually started, was queued, or is blocked."
            ),
        )
        tx.execute("UPDATE xp_followups SET admission_id=? WHERE id=?", (op_id, row["id"]))


class Watches:
    """Attach feed readers to the workspace's durable registrations on its dispatch loop."""

    def __init__(self, store: Store, read: ReadChanges, group: asyncio.TaskGroup):
        self.store = store
        self.read = read
        self.group = group
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.task_waits: dict[tuple[Path, int, str], asyncio.Task[None]] = {}
        self.version: int | None = None

    def sync(self) -> None:
        # Tool subprocesses commit through their own SQLite connections. Use the connection's
        # data version to notice their writes on the same loop that dispatches chat admissions.
        version = self.store.one("PRAGMA data_version")[0]
        if version == self.version:
            return
        self.version = version
        if settings.get(self.store, "paused"):
            self.cancel()
            return
        self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
        for row in self.store.q(
            "SELECT f.*,s.profile,s.kind FROM xp_followups f JOIN sessions s ON s.id=f.session_id"
            " WHERE f.admission_id IS NULL ORDER BY f.created_at,f.id"
        ):
            if row["id"] not in self.tasks:
                self.tasks[row["id"]] = self.group.create_task(
                    deliver(self.read, self.store.path, row)
                )

    def sync_task_waits(self, stores: dict[str, Store]) -> None:
        """Follow held Tasks' XP waits after the launching attempt has ended."""
        if settings.get(self.store, "paused"):
            return
        active = set()
        for row in self.store.q(
            "SELECT t.coworld_id,t.task_id,t.session_id FROM task_assignments t"
            " JOIN client_admissions a ON a.op_id=t.current_attempt_id"
            " WHERE t.state='held' AND a.status IN ('completed','failed','interrupted','cancelled')"
            " AND a.execution_uncertain=0 AND a.resolution_required=0 AND a.advanced_at IS NOT NULL"
        ):
            store = stores.get(row["coworld_id"])
            if store is None:
                continue
            for wait in store.q(
                "SELECT x.* FROM task_xp_waits x JOIN tasks t ON t.id=x.task_id"
                " WHERE t.id=? AND t.status='in-progress' AND t.held_by=?",
                (row["task_id"], row["session_id"]),
            ):
                key = (store.path, wait["id"], wait["request_ids"])
                active.add(key)
                if key not in self.task_waits:
                    self.task_waits[key] = self.group.create_task(
                        self.resolve_task_wait(store, wait)
                    )
        for key in self.task_waits.keys() - active:
            self.task_waits.pop(key).cancel()

    async def resolve_task_wait(self, store: Store, wait: Row) -> None:
        from research_core.platform.experience_changes import MAX_ROOTS, wait_for_terminal

        ids = json.loads(wait["request_ids"])
        async with asyncio.TaskGroup() as group:
            batches = [
                group.create_task(wait_for_terminal(self.read, selection))
                for selection in batched(ids, MAX_ROOTS)
            ]
        experiment = wait["experiment"]
        reason = (
            f"{experiment} requests finished."
            if all(batch.result() for batch in batches)
            else f"Some {experiment} requests are no longer accessible. Check what finished."
        )
        with store.tx(Actor.harness()) as tx:
            deleted = tx.execute(
                "DELETE FROM task_xp_waits WHERE id=? AND request_ids=?"
                " AND EXISTS (SELECT 1 FROM tasks WHERE id=task_xp_waits.task_id"
                " AND status='in-progress')",
                (wait["id"], wait["request_ids"]),
            )
            if deleted.rowcount:
                tx.event(
                    "task.wait_resolved",
                    f"T-{wait['task_id']}",
                    {
                        "kind": "xp",
                        "experiment": experiment,
                        "ref": ids,
                        "note": f"{reason} Evaluate with xp.status and report the findings.",
                    },
                )

    def cancel(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()
        for task in self.task_waits.values():
            task.cancel()
        self.task_waits.clear()
