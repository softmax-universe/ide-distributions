"""Sessions (B.5): rows in the workspace store only, upserted; runs and stops recorded."""

from __future__ import annotations

import pytest

from research_core.store import sessions
from research_core.store.errors import NotFound, StoreError

T0 = "2026-10-28T21:04:05.123456Z"
T1 = "2026-10-28T21:05:05.123456Z"
SID = "api_1761690245_0badcafe"


def events(store):
    return [(e.kind, e.subject, e.payload) for e in store.events_after(0)]


def test_register_inserts_and_emits_session_started(workspace_store, harness):
    record = sessions.register(
        workspace_store,
        harness,
        session_id=SID,
        profile="researcher",
        kind="task",
        coworld_id="cow_x",
        task_id=4,
        parent="api_1_orch",
    )
    assert record == {
        "id": SID,
        "profile": "researcher",
        "kind": "task",
        "coworld_id": "cow_x",
        "task_id": 4,
        "goal_id": None,
        "event": None,
        "parent": "api_1_orch",
        "run_id": None,
        "attempts": 0,
        "started": T0,
        "ended": None,
        "last_seen": T0,
        "session_context": None,
        "client_capabilities": None,
        "client": None,
        "drained": False,
        "stopped_by": None,
        "created": True,
    }
    assert events(workspace_store) == [
        (
            "session.started",
            SID,
            {
                "profile": "researcher",
                "kind": "task",
                "coworld_id": "cow_x",
                "task_id": 4,
                "goal_id": None,
                "event": None,
                "parent": "api_1_orch",
            },
        )
    ]
    assert workspace_store.unmarked_changes() == []


def test_register_again_updates_without_a_second_event(workspace_store, harness, clock):
    sessions.register(
        workspace_store,
        harness,
        session_id=SID,
        profile="orchestrator",
        kind="chat",
        coworld_id="cow_x",
    )
    sessions.end(workspace_store, harness, SID)
    clock.advance(60)
    record = sessions.register(
        workspace_store,
        harness,
        session_id=SID,
        profile="orchestrator",
        kind="chat",
        coworld_id="cow_x",
        session_context={"client": "coplay"},
        client_capabilities=["ui.notify"],
    )
    assert record["created"] is False
    assert record["coworld_id"] == "cow_x"
    assert record["session_context"] == {"client": "coplay"}
    assert record["client_capabilities"] == ["ui.notify"]
    assert record["started"] == T0
    assert record["last_seen"] == T1
    # A re-registered Session is live again.
    assert record["ended"] is None
    kinds = [kind for kind, _, _ in events(workspace_store)]
    assert kinds == ["session.started", "session.ended"]


def test_register_validates_everything_at_once(workspace_store, harness):
    with pytest.raises(StoreError) as info:
        sessions.register(workspace_store, harness, session_id="", profile="", kind="cron")
    assert len(info.value.problems) == 3
    with pytest.raises(StoreError) as info:
        sessions.register(workspace_store, harness, session_id=SID, profile="r", kind="task")
    assert info.value.problems == ["a task Session names its task_id"]
    with pytest.raises(StoreError):
        sessions.register(
            workspace_store, harness, session_id=SID, profile="r", kind="chat", task_id="4"
        )
    assert sessions.read(workspace_store, SID) is None


def test_set_run_counts_attempts_and_makes_the_session_live(workspace_store, harness, clock):
    sessions.register(workspace_store, harness, session_id=SID, profile="r", kind="task", task_id=1)
    record = sessions.set_run(workspace_store, harness, SID, "run_1")
    assert record["run_id"] == "run_1"
    assert record["attempts"] == 1
    sessions.end(workspace_store, harness, SID)
    sessions.set_stopped_by(workspace_store, harness, SID, "watcher:replace")
    clock.advance(60)
    record = sessions.set_run(workspace_store, harness, SID, "run_2")
    assert record["attempts"] == 2
    assert record["ended"] is None
    assert record["stopped_by"] is None
    assert record["last_seen"] == T1
    record = sessions.set_run(workspace_store, harness, SID, "run_7", attempts=7)
    assert record["attempts"] == 7
    with pytest.raises(NotFound):
        sessions.set_run(workspace_store, harness, "nope", "run_1")
    with pytest.raises(StoreError):
        sessions.set_run(workspace_store, harness, SID, "")
    # No event kind exists for a run start: the session.started event is the only one.
    assert [kind for kind, _, _ in events(workspace_store)] == ["session.started", "session.ended"]


def test_end_emits_once(workspace_store, harness, clock):
    sessions.register(workspace_store, harness, session_id=SID, profile="r", kind="task", task_id=1)
    sessions.set_run(workspace_store, harness, SID, "run_1")
    clock.advance(60)
    record = sessions.end(workspace_store, harness, SID)
    assert record["ended"] == T1
    clock.advance(60)
    again = sessions.end(workspace_store, harness, SID)
    assert again["ended"] == T1
    assert events(workspace_store)[1] == (
        "session.ended",
        SID,
        {"profile": "r", "kind": "task", "task_id": 1, "run_id": "run_1", "stopped_by": None},
    )
    assert len(events(workspace_store)) == 2
    with pytest.raises(NotFound):
        sessions.end(workspace_store, harness, "nope")


def test_read_and_for_task(workspace_store, harness, clock):
    sessions.register(
        workspace_store, harness, session_id="s1", profile="r", kind="task", task_id=1
    )
    clock.advance(1)
    sessions.register(
        workspace_store,
        harness,
        session_id="s2",
        profile="r",
        kind="task",
        task_id=1,
        coworld_id="cow_x",
    )
    sessions.register(
        workspace_store,
        harness,
        session_id="s3",
        profile="r",
        kind="task",
        task_id=1,
        coworld_id="cow_y",
    )
    sessions.register(workspace_store, harness, session_id="s4", profile="o", kind="chat")
    assert sessions.read(workspace_store, "s4")["kind"] == "chat"
    assert sessions.read(workspace_store, "none") is None
    assert [s["id"] for s in sessions.for_task(workspace_store, 1)] == ["s1", "s2", "s3"]
    assert [s["id"] for s in sessions.for_task(workspace_store, 1, coworld_id="cow_x")] == ["s2"]
    assert sessions.for_task(workspace_store, 2) == []


def test_mark_drained(workspace_store, harness):
    sessions.register(
        workspace_store, harness, session_id="s1", profile="r", kind="task", task_id=1
    )
    sessions.register(
        workspace_store, harness, session_id="s2", profile="r", kind="task", task_id=2
    )
    drained = sessions.mark_drained(workspace_store, harness, ["s1", "s2"], True)
    assert [(s["id"], s["drained"]) for s in drained] == [("s1", True), ("s2", True)]
    resumed = sessions.mark_drained(workspace_store, harness, ["s1"], False)
    assert [(s["id"], s["drained"]) for s in resumed] == [("s1", False)]
    assert sessions.read(workspace_store, "s2")["drained"] is True
    with pytest.raises(NotFound) as info:
        sessions.mark_drained(workspace_store, harness, ["s1", "x", "y"], True)
    assert info.value.problems == ["session x does not exist", "session y does not exist"]
    assert sessions.mark_drained(workspace_store, harness, [], True) == []


def test_set_stopped_by_round_trip(workspace_store, harness):
    sessions.register(workspace_store, harness, session_id=SID, profile="r", kind="task", task_id=1)
    record = sessions.set_stopped_by(workspace_store, harness, SID, "watcher:drain")
    assert record["stopped_by"] == "watcher:drain"
    assert sessions.read(workspace_store, SID)["stopped_by"] == "watcher:drain"
    ended = sessions.end(workspace_store, harness, SID)
    assert ended["stopped_by"] == "watcher:drain"
    assert events(workspace_store)[1][2]["stopped_by"] == "watcher:drain"
    cleared = sessions.set_stopped_by(workspace_store, harness, SID, None)
    assert cleared["stopped_by"] is None
    with pytest.raises(StoreError):
        sessions.set_stopped_by(workspace_store, harness, SID, "")
    with pytest.raises(NotFound):
        sessions.set_stopped_by(workspace_store, harness, "nope", "watcher:abandon")


def test_sessions_live_only_in_the_workspace_store(coworld_store, harness):
    with pytest.raises(StoreError) as info:
        sessions.register(coworld_store, harness, session_id=SID, profile="r", kind="chat")
    assert "workspace store" in info.value.problems[0]
    with pytest.raises(StoreError):
        sessions.read(coworld_store, SID)
