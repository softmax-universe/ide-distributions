"""Deliver experiment completions to the originating agent."""

from __future__ import annotations

import asyncio
import json
from itertools import batched
from pathlib import Path
from sqlite3 import Row
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from research_core.store import admissions, sessions, settings, tasks
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
        else "Decide whether the outcome warrants a user notification."
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
                f"to check the actual results, including failed or missing results. {audience} "
                "If evaluation is still running, use its resume "
                "token to finish it before reporting. Follow the user's earlier instructions."
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
        self.task_waits: dict[tuple[Path, int, str, str], asyncio.Task[None]] = {}
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
            task = store.one(
                "SELECT waiting_on FROM tasks WHERE id=? AND status='in-progress' AND held_by=?",
                (row["task_id"], row["session_id"]),
            )
            if task is None or task["waiting_on"] is None:
                continue
            waiting = json.loads(task["waiting_on"])
            if waiting["kind"] != "xp":
                continue
            key = (store.path, row["task_id"], row["session_id"], task["waiting_on"])
            active.add(key)
            if key not in self.task_waits:
                self.task_waits[key] = self.group.create_task(
                    self.resolve_task_wait(
                        store, row["task_id"], row["session_id"], task["waiting_on"], waiting["ref"]
                    )
                )
        for key in self.task_waits.keys() - active:
            self.task_waits.pop(key).cancel()

    async def resolve_task_wait(
        self, store: Store, task_id: int, holder: str, waiting: str, refs: list[str] | str | None
    ) -> None:
        from research_core.platform.experience_changes import MAX_ROOTS, wait_for_terminal

        ids = [refs] if isinstance(refs, str) else refs or []
        async with asyncio.TaskGroup() as group:
            batches = [
                group.create_task(wait_for_terminal(self.read, selection))
                for selection in batched(ids, MAX_ROOTS)
            ]
        reason = (
            "Experiment requests finished."
            if all(batch.result() for batch in batches)
            else "Some experiment requests are no longer accessible. Check what finished."
        )

        actor = Actor.harness()
        with store.atomic(actor) as tx:
            if tx.one(
                "SELECT 1 FROM tasks WHERE id=? AND status='in-progress'"
                " AND held_by=? AND waiting_on=?",
                (task_id, holder, waiting),
            ):
                tasks.resolve_wait(
                    store,
                    actor,
                    task_id,
                    note=f"{reason} Use xp.status to evaluate, then report to the Orchestrator.",
                )

    def cancel(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()
        for task in self.task_waits.values():
            task.cancel()
        self.task_waits.clear()
