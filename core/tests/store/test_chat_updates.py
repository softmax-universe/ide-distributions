"""Chat updates (``chat_update``): recorded by a wake, delivered by the coordinator into a chat."""

from __future__ import annotations

import pytest

from research_core.store import admissions, chat_updates, notices, sessions, settings
from research_core.store.errors import StoreError

COWORLD = "cow_x"


def chat(store, harness, session_id, *, coworld_id=COWORLD, parent=None):
    sessions.register(
        store,
        harness,
        session_id=session_id,
        profile="orchestrator",
        kind="chat",
        coworld_id=coworld_id,
        parent=parent,
    )


def tell(store, actor, text, refs=(), coworld_id=COWORLD, category="outcome"):
    return chat_updates.add(
        store, actor, category=category, text=text, refs=list(refs), coworld_id=coworld_id
    )


def user_message(store, harness, session_id, key):
    admissions.enqueue(
        store,
        harness,
        key=key,
        session_id=session_id,
        profile="orchestrator",
        text="hello",
        origin="client",
    )


def update_admissions(store):
    return store.q(
        "SELECT * FROM client_admissions WHERE substr(native_key, 1, 13)='chat-updates:'"
        " ORDER BY rowid"
    )


def test_add_records_a_pending_update_and_a_metadata_event(workspace_store, session_actor):
    record = tell(workspace_store, session_actor, "V-7 merged", ["V-7"])["update"]
    assert record["id"] == 1 and record["refs"] == ["V-7"] and record["category"] == "outcome"
    assert record["admission_id"] is None and record["notice_id"] is None
    assert record["created_by"] == "session:api_1761690245_0badcafe"
    [event] = workspace_store.events_after(0)
    assert (event.kind, event.subject) == ("chat_update.recorded", "chat_update:1")
    assert event.payload == {"coworld_id": COWORLD, "category": "outcome", "refs": ["V-7"]}
    assert [u["text"] for u in chat_updates.pending(workspace_store, COWORLD)] == ["V-7 merged"]
    assert chat_updates.pending(workspace_store, None) == []


@pytest.mark.parametrize(
    ("category", "text", "refs", "problem"),
    [
        ("outcome", "  ", [], "non-empty"),
        ("outcome", "x" * (chat_updates.MAX_TEXT + 1), [], "at most 4000 characters"),
        ("outcome", "ok", ["T-1", ""], "list of record references"),
        ("outcome", "ok", [f"T-{n}" for n in range(1, 22)], "at most 20 records"),
        (
            "news",
            "ok",
            [],
            "category is one of decision, outcome, submission, failure, merge, progress",
        ),
        # A disabled category is validated the same before it becomes a notice.
        ("merge", "", [], "non-empty"),
    ],
)
def test_add_refuses_empty_or_oversized_input(
    workspace_store, session_actor, category, text, refs, problem
):
    with pytest.raises(StoreError) as refused:
        tell(workspace_store, session_actor, text, refs, category=category)
    assert problem in str(refused.value)
    assert workspace_store.one("SELECT COUNT(*) FROM chat_updates")[0] == 0
    assert workspace_store.one("SELECT COUNT(*) FROM notices")[0] == 0


def test_a_category_the_setting_leaves_out_becomes_a_notice_at_once(
    workspace_store, harness, session_actor
):
    assert settings.get(workspace_store, "chat_updates.categories") == [
        "decision",
        "outcome",
        "submission",
        "failure",
    ]
    result = tell(workspace_store, session_actor, "V-7 merged to main", ["V-7"], category="merge")
    assert set(result) == {"notice"} and result["notice"]["id"] == "N-1"
    assert [n["text"] for n in notices.unread(workspace_store, coworld_id=COWORLD)] == [
        "V-7 merged to main"
    ]
    assert chat_updates.pending(workspace_store, COWORLD) == []

    settings.update(workspace_store, harness, "chat_updates.categories", ["merge"], cites="msg:1")
    assert "update" in tell(workspace_store, session_actor, "V-8 merged", category="merge")
    assert "notice" in tell(workspace_store, session_actor, "X-2 closed", category="outcome")
    settings.update(workspace_store, harness, "chat_updates.categories", [], cites="msg:2")
    assert "notice" in tell(workspace_store, session_actor, "Decide on V-9", category="decision")


def test_progress_is_always_a_notice_and_never_reaches_the_chat(
    workspace_store, harness, session_actor, clock
):
    chat(workspace_store, harness, "chat1")
    every = ["decision", "outcome", "submission", "failure", "merge"]
    settings.update(workspace_store, harness, "chat_updates.categories", every, cites="msg:1")
    result = tell(workspace_store, session_actor, "Smoke test passed", ["T-3"], category="progress")
    assert set(result) == {"notice"} and result["notice"]["urgency"] == "normal"
    assert [n["text"] for n in notices.unread(workspace_store, coworld_id=COWORLD)] == [
        "Smoke test passed"
    ]
    assert workspace_store.one("SELECT COUNT(*) FROM chat_updates")[0] == 0
    clock.advance(int(chat_updates.MAX_WAIT.total_seconds()))
    assert chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD) == {}
    assert update_admissions(workspace_store) == []


def test_delivery_waits_for_quiet_then_sends_one_admission(
    workspace_store, harness, session_actor, clock
):
    chat(workspace_store, harness, "chat1")
    tell(workspace_store, session_actor, "T-3 finished", ["T-3"])
    clock.advance(60)
    tell(workspace_store, session_actor, "Submit V-7?", ["V-7", "X-2"], category="decision")
    clock.advance(119)
    assert chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD) == {}
    assert update_admissions(workspace_store) == []

    clock.advance(1)
    report = chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD)
    [admission] = update_admissions(workspace_store)
    assert report == {
        "delivered": {"session": "chat1", "admission": admission["op_id"], "updates": 2}
    }
    assert admission["native_key"] == "chat-updates:1-2"
    assert admission["origin"] == "followup" and admission["status"] == "queued"
    assert admission["session_id"] == "chat1" and admission["profile"] == "orchestrator"
    text = admission["message"]
    assert text.startswith(chat_updates.HEADER)
    assert "1. outcome (2026-10-28 21:04 UTC; about T-3): T-3 finished" in text
    assert "2. decision (2026-10-28 21:05 UTC; about V-7, X-2): Submit V-7?" in text
    rows = workspace_store.q("SELECT admission_id FROM chat_updates")
    assert {row["admission_id"] for row in rows} == {admission["op_id"]}
    delivered = [e for e in workspace_store.events_after(0) if e.kind == "chat_update.delivered"]
    assert [(e.subject, e.payload) for e in delivered] == [
        (
            "chat-updates:1-2",
            {
                "coworld_id": COWORLD,
                "via": "chat",
                "session_id": "chat1",
                "admission_id": admission["op_id"],
                "updates": 2,
            },
        )
    ]
    assert chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD) == {}


def test_a_steady_trickle_is_delivered_after_ten_minutes(
    workspace_store, harness, session_actor, clock
):
    chat(workspace_store, harness, "chat1")
    for minute in range(10):
        tell(workspace_store, session_actor, f"update {minute}")
        assert chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD) == {}
        clock.advance(60)
    report = chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD)
    assert report["delivered"]["updates"] == 10


def test_a_queued_update_admission_holds_the_next_until_it_leaves_the_queue(
    workspace_store, harness, session_actor, clock
):
    chat(workspace_store, harness, "chat1")
    tell(workspace_store, session_actor, "first")
    clock.advance(120)
    first = chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD)["delivered"]
    tell(workspace_store, session_actor, "second")
    clock.advance(120)
    held = chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD)
    assert held == {"waiting": {"session": "chat1", "updates": 1}}
    assert len(update_admissions(workspace_store)) == 1

    with workspace_store.tx(harness) as tx:
        tx.execute(
            "UPDATE client_admissions SET status='running' WHERE op_id=?", (first["admission"],)
        )
    second = chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD)["delivered"]
    assert second["updates"] == 1 and second["admission"] != first["admission"]
    assert [row["native_key"] for row in update_admissions(workspace_store)] == [
        "chat-updates:1-1",
        "chat-updates:2-2",
    ]


def test_the_target_is_the_coworlds_most_recently_used_top_level_chat(
    workspace_store, harness, session_actor, clock
):
    chat(workspace_store, harness, "older")
    clock.advance(5)
    chat(workspace_store, harness, "newer")
    clock.advance(5)
    chat(workspace_store, harness, "child", parent="newer")
    chat(workspace_store, harness, "elsewhere", coworld_id="cow_other")
    clock.advance(5)
    user_message(workspace_store, harness, "older", "typed-later")
    sessions.register(
        workspace_store, harness, session_id="w1", profile="orchestrator", kind="wake",
        coworld_id=COWORLD,
    )  # fmt: skip
    tell(workspace_store, session_actor, "news")
    clock.advance(120)
    report = chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD)
    assert report["delivered"]["session"] == "older"


def test_no_chat_turns_the_updates_into_notices(workspace_store, harness, session_actor, clock):
    chat(workspace_store, harness, "elsewhere", coworld_id="cow_other")
    tell(workspace_store, session_actor, "T-3 finished", ["T-3"])
    tell(workspace_store, session_actor, "V-7 merged")
    clock.advance(120)
    assert chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD) == {"noticed": 2}
    unread = notices.unread(workspace_store, coworld_id=COWORLD)
    assert [(n["text"], n["refs"]) for n in unread] == [
        ("T-3 finished", ["T-3"]),
        ("V-7 merged", []),
    ]
    assert [row["notice_id"] for row in workspace_store.q("SELECT * FROM chat_updates")] == [1, 2]
    assert update_admissions(workspace_store) == []
    assert chat_updates.pending(workspace_store, COWORLD) == []
    assert chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD) == {}


def test_a_restart_after_commit_cannot_deliver_twice(
    workspace_store, harness, session_actor, clock, tmp_path
):
    from research_core.store.db import Store

    chat(workspace_store, harness, "chat1")
    tell(workspace_store, session_actor, "news")
    clock.advance(120)
    chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD)
    workspace_store.close()
    with Store.open(tmp_path / ".state" / "store.db", kind="workspace", clock=clock) as reopened:
        assert chat_updates.deliver(reopened, harness, coworld_id=COWORLD) == {}
        assert len(update_admissions(reopened)) == 1


def test_replaying_a_delivery_reuses_its_admission(workspace_store, harness, session_actor, clock):
    """A delivery whose receipt was lost replays the same key and text, so the durable inbox
    returns the admission it already holds rather than queueing a second one."""
    chat(workspace_store, harness, "chat1")
    tell(workspace_store, session_actor, "news")
    clock.advance(120)
    first = chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD)["delivered"]
    with workspace_store.tx(harness) as tx:
        tx.execute("UPDATE chat_updates SET admission_id=NULL")
        tx.execute("UPDATE client_admissions SET status='completed'")
    again = chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD)["delivered"]
    assert again["admission"] == first["admission"]
    assert len(update_admissions(workspace_store)) == 1


def test_a_batch_carries_at_most_twenty_updates(workspace_store, harness, session_actor):
    chat(workspace_store, harness, "chat1")
    for number in range(chat_updates.MAX_BATCH + 3):
        tell(workspace_store, session_actor, f"update {number}")
    report = chat_updates.deliver(workspace_store, harness, coworld_id=COWORLD)
    assert report["delivered"]["updates"] == chat_updates.MAX_BATCH
    assert len(chat_updates.pending(workspace_store, COWORLD)) == 3
