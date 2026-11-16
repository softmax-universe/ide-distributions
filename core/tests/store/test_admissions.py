"""Durable execution intents before any transport or scheduler can act."""

import json
import sqlite3

import pytest

from research_core.store import admissions, sessions
from research_core.store.errors import StoreError


def enqueue(store, actor, **changes):
    return admissions.enqueue(
        store,
        actor,
        key="one-intent",
        session_id="worker-1",
        profile="researcher",
        text="Repair the task",
        origin="assignment",
        **changes,
    )


def test_replayed_intent_keeps_identity_and_one_durable_handoff(workspace_store, harness):
    first = enqueue(workspace_store, harness)
    assert enqueue(workspace_store, harness) == first
    [row] = workspace_store.q("SELECT * FROM client_admissions")
    assert row["op_id"] == first and row["status"] == "queued" and not row["dispatch_started"]
    assert json.loads(row["payload"])["input"] == "Repair the task"
    assert [event.kind for event in workspace_store.events_after(0)] == ["execution.queued"]
    assert workspace_store.unmarked_changes() == []


def test_changed_body_cannot_reuse_a_durable_execution_identity(workspace_store, harness):
    enqueue(workspace_store, harness)
    with pytest.raises(StoreError, match="different payload"):
        admissions.enqueue(
            workspace_store,
            harness,
            key="one-intent",
            session_id="worker-1",
            profile="researcher",
            text="Different work",
            origin="assignment",
        )
    assert workspace_store.one("SELECT count(*) FROM client_admissions")[0] == 1


def test_changed_origin_cannot_reuse_a_durable_execution_identity(workspace_store, harness):
    enqueue(workspace_store, harness)
    with pytest.raises(StoreError, match="different payload"):
        admissions.enqueue(
            workspace_store,
            harness,
            key="one-intent",
            session_id="worker-1",
            profile="researcher",
            text="Repair the task",
            origin="answer",
        )
    [row] = workspace_store.q("SELECT origin FROM client_admissions")
    assert row["origin"] == "assignment"


def test_unknown_origin_cannot_leave_a_wedged_intent(workspace_store, harness):
    with pytest.raises(StoreError, match="origin must be one of"):
        admissions.enqueue(
            workspace_store,
            harness,
            key="typo",
            session_id="worker-1",
            profile="researcher",
            text="Repair",
            origin="assigment",
        )
    assert workspace_store.one("SELECT count(*) FROM client_admissions")[0] == 0
    assert workspace_store.last_seq() == 0


def test_release_is_not_a_blocked_execution_and_has_no_unowned_resolution_state(
    workspace_store, harness
):
    op_id = enqueue(workspace_store, harness)
    with workspace_store.tx(harness) as tx:
        tx.execute(
            "INSERT INTO task_assignments"
            " (coworld_id,task_id,op_id,session_id,profile,task_updated,rationale,context,state)"
            " VALUES ('world',1,?,'worker-1','researcher',?,'Fits','Repair','release_requested')",
            (op_id, tx.at),
        )
    assert admissions.problems(workspace_store) == []
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        with workspace_store.tx(harness) as tx:
            tx.execute("UPDATE task_assignments SET state='needs_resolution'")
    with workspace_store.tx(harness) as tx:
        tx.execute(
            "UPDATE client_admissions SET resolution_required=1,error='Legacy receipt unknown'"
            " WHERE op_id=?",
            (op_id,),
        )
    assert admissions.problems(workspace_store) == [
        {"op_id": op_id, "profile": "researcher", "reason": "Legacy receipt unknown"}
    ]


def test_uncommitted_intent_and_wakeup_are_rolled_back_together(workspace_store, harness):
    with pytest.raises(RuntimeError, match="crash before commit"):
        with workspace_store.tx(harness) as tx:
            admissions.enqueue_in_transaction(
                tx,
                key="one-intent",
                session_id="worker-1",
                profile="researcher",
                text="Repair",
                origin="assignment",
            )
            raise RuntimeError("crash before commit")
    assert workspace_store.one("SELECT count(*) FROM client_admissions")[0] == 0
    assert workspace_store.last_seq() == 0


def test_uncertain_execution_reserves_capacity_after_delivery_closes(workspace_store, harness):
    sessions.register(
        workspace_store,
        harness,
        session_id="worker-1",
        profile="researcher",
        kind="task",
        coworld_id="world",
        task_id=1,
    )
    op_id = enqueue(workspace_store, harness)
    with workspace_store.tx(harness) as tx:
        tx.execute(
            "UPDATE client_admissions SET status='interrupted',execution_uncertain=1 WHERE op_id=?",
            (op_id,),
        )
    assert admissions.reserved_workers(workspace_store) == 1
    assert admissions.restart_required(workspace_store)
    with workspace_store.tx(harness) as tx:
        tx.execute("UPDATE client_admissions SET execution_uncertain=0 WHERE op_id=?", (op_id,))
    assert admissions.reserved_workers(workspace_store) == 0
    assert not admissions.restart_required(workspace_store)


def test_work_status_finds_open_admissions_through_an_index(workspace_store, harness):
    """``/status`` asks for this every second. Without the index it reads every stored admission
    with its message text: 0.4 s on a workspace holding 7,000 of them. The index holds only the
    rows that match its own condition, so a condition added to the query alone scans again."""
    op_id = enqueue(workspace_store, harness)
    statements = []
    workspace_store.conn.set_trace_callback(statements.append)
    try:
        obligations = admissions.work_status(workspace_store)
    finally:
        workspace_store.conn.set_trace_callback(None)
    assert [row["attempt_id"] for row in obligations] == [op_id]
    (query,) = [sql for sql in statements if "FROM client_admissions a" in sql]
    plan = [row[3] for row in workspace_store.conn.execute("EXPLAIN QUERY PLAN " + query)]
    assert any("client_admissions_open" in step for step in plan), plan
