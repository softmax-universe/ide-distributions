"""AIDE owns the handoff from platform experiment completion to the originating chat."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from research_core.store import admissions, sessions

if TYPE_CHECKING:
    from research_core.store.actors import Actor
    from research_core.store.db import Store
    from research_core.watcher.context import TickContext


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


def advance(ctx: TickContext) -> list[str]:
    """Observe terminal requests and atomically hand each batch to the regular chat queue."""
    queued = []
    for row in ctx.wstore.q(
        "SELECT f.*,s.profile FROM xp_followups f JOIN sessions s ON s.id=f.session_id"
        " WHERE f.admission_id IS NULL ORDER BY f.checked_at,f.created_at,f.id"
    ):
        # Advance the polling order before calling the platform. A failed lookup is logged by
        # the workflow supervisor and cannot starve other batches on the next tick.
        with ctx.wstore.tx(ctx.actor) as tx:
            tx.execute("UPDATE xp_followups SET checked_at=? WHERE id=?", (tx.at, row["id"]))
        if not all(
            ctx.platform.xp_get(request_id).terminal
            for request_id in json.loads(row["request_ids"])
        ):
            continue
        experiment = row["experiment"]
        with ctx.wstore.tx(ctx.actor) as tx:
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
        queued.append(op_id)
    return queued
