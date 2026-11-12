"""Notices (A.8): the store side; workspace store only; each adds a notice event."""

from __future__ import annotations

import pytest

from research_core.store import notices
from research_core.store.errors import NotFound, StoreError

T0 = "2026-10-28T21:04:05.123456Z"
T1 = "2026-10-28T21:05:05.123456Z"


def test_add_records_a_notice_and_an_event(workspace_store, session_actor):
    record = notices.add(
        workspace_store,
        session_actor,
        text="V-7 was submitted to league_1",
        refs=["V-7"],
        urgency="low",
    )
    assert record == {
        "id": "N-1",
        "coworld_id": None,
        "text": "V-7 was submitted to league_1",
        "refs": ["V-7"],
        "urgency": "low",
        "created": T0,
        "created_by": "session:api_1761690245_0badcafe",
        "read_at": None,
        "read_by": None,
    }
    events = workspace_store.events_after(0)
    assert [(e.kind, e.subject, e.payload) for e in events] == [
        (
            "notice",
            "N-1",
            {
                "text": "V-7 was submitted to league_1",
                "refs": ["V-7"],
                "urgency": "low",
                "coworld_id": None,
            },
        )
    ]
    assert workspace_store.unmarked_changes() == []


def test_add_defaults_and_validation(workspace_store, harness):
    record = notices.add(workspace_store, harness, text="drive disconnected")
    assert record["refs"] == []
    assert record["urgency"] == "normal"
    with pytest.raises(StoreError) as info:
        notices.add(workspace_store, harness, text="", refs=["V-7", 3], urgency="loud")
    assert len(info.value.problems) == 3
    with pytest.raises(StoreError):
        notices.add(workspace_store, harness, text="x", refs="V-7")
    assert len(notices.unread(workspace_store)) == 1


def test_unread_returns_the_newest_in_chronological_order(workspace_store, harness, clock):
    for n in range(5):
        notices.add(workspace_store, harness, text=f"n{n}")
        clock.advance(1)
    assert [n["text"] for n in notices.unread(workspace_store)] == ["n0", "n1", "n2", "n3", "n4"]
    # A limit drops the oldest, and what remains still reads oldest first.
    assert [n["text"] for n in notices.unread(workspace_store, limit=2)] == ["n3", "n4"]
    with pytest.raises(StoreError):
        notices.unread(workspace_store, limit=0)


def test_mark_read_sets_who_and_when_once(workspace_store, harness, clock):
    notices.add(workspace_store, harness, text="a")
    notices.add(workspace_store, harness, text="b")
    notices.add(workspace_store, harness, text="c")
    clock.advance(60)
    marked = notices.mark_read(workspace_store, harness, [1, 2], by="client:coplay")
    assert [(n["id"], n["read_at"], n["read_by"]) for n in marked] == [
        ("N-1", T1, "client:coplay"),
        ("N-2", T1, "client:coplay"),
    ]
    assert [n["id"] for n in notices.unread(workspace_store)] == ["N-3"]
    clock.advance(60)
    again = notices.mark_read(workspace_store, harness, [1, 3], by="user:u1")
    assert [(n["id"], n["read_at"], n["read_by"]) for n in again] == [
        ("N-1", T1, "client:coplay"),
        ("N-3", "2026-10-28T21:06:05.123456Z", "user:u1"),
    ]
    assert notices.unread(workspace_store) == []
    # Reading is bookkeeping: only the three notice events exist.
    assert [e.kind for e in workspace_store.events_after(0)] == ["notice"] * 3
    assert notices.mark_read(workspace_store, harness, [], by="x") == []


def test_mark_read_refuses_unknown_ids_and_an_empty_reader(workspace_store, harness):
    notices.add(workspace_store, harness, text="a")
    with pytest.raises(NotFound) as info:
        notices.mark_read(workspace_store, harness, [1, 8, 9], by="x")
    assert info.value.problems == ["N-8 does not exist", "N-9 does not exist"]
    with pytest.raises(StoreError):
        notices.mark_read(workspace_store, harness, [1], by="")
    with pytest.raises(StoreError):
        notices.mark_read(workspace_store, harness, ["N-1"], by="x")
    assert len(notices.unread(workspace_store)) == 1


def test_notices_live_only_in_the_workspace_store(coworld_store, harness):
    with pytest.raises(StoreError) as info:
        notices.add(coworld_store, harness, text="x")
    assert "workspace store" in info.value.problems[0]
    with pytest.raises(StoreError):
        notices.unread(coworld_store)
