"""AIDE owns the handoff from platform experiment completion to the originating chat."""

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
    if session is None or session["kind"] != "chat":
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
        for ids in batched(json.loads(row["request_ids"]), MAX_ROOTS):
            watches.create_task(wait_for_terminal(read, ids))

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
                f"The watched experience requests for {experiment} "
                "have reached terminal states. "
                "This is an automatic follow-up. Read the experiment and use xp.status "
                "to check the actual results. Report the outcome in this chat, including "
                "failed or missing results. If evaluation is still running, use its resume "
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
            "SELECT f.*,s.profile FROM xp_followups f JOIN sessions s ON s.id=f.session_id"
            " WHERE f.admission_id IS NULL ORDER BY f.created_at,f.id"
        ):
            if row["id"] not in self.tasks:
                self.tasks[row["id"]] = self.group.create_task(
                    deliver(self.read, self.store.path, row)
                )

    def cancel(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()
