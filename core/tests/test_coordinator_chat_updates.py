"""The coordinator delivers a wake's ``ui.tell`` updates into the Coworld's latest chat."""

from __future__ import annotations

from research_core.store import chat_updates, notices, sessions
from research_core.store.actors import Actor
from tests.watcher._support import (
    COWORLD,
    HARNESS,
    advance,
    coordinator,
    dispatch_inputs,
    make_ctx,
    set_setting,
)

WAKE = Actor.session("wake1")


def tell(ctx, text, category="outcome"):
    return chat_updates.add(ctx.wstore, WAKE, category=category, text=text, coworld_id=COWORLD)


def chat(ctx, session_id="chat1"):
    sessions.register(
        ctx.wstore,
        HARNESS,
        session_id=session_id,
        profile="orchestrator",
        kind="chat",
        coworld_id=COWORLD,
    )


def followups(ctx):
    return ctx.wstore.q("SELECT * FROM client_admissions WHERE origin='followup' ORDER BY rowid")


def test_advance_coalesces_a_burst_into_one_followup_after_the_quiet_window(tmp_path):
    ctx = make_ctx(tmp_path)
    chat(ctx)
    owner = coordinator(ctx)
    tell(ctx, "T-3 finished")
    advance(ctx, 30)
    tell(ctx, "V-7 merged", category="merge")  # off by default: a notice now, not in the batch
    tell(ctx, "Submit V-7?", category="decision")
    advance(ctx, 100)
    assert owner.advance(ctx)["chat_updates"] == {}
    assert followups(ctx) == []
    advance(ctx, 20)
    report = owner.advance(ctx)["chat_updates"]
    assert report["delivered"]["updates"] == 2
    [admission] = followups(ctx)
    assert admission["session_id"] == "chat1" and admission["native_key"] == "chat-updates:1-2"
    assert "T-3 finished" in admission["message"] and "Submit V-7?" in admission["message"]
    assert "V-7 merged" not in admission["message"]
    assert [n["text"] for n in notices.unread(ctx.wstore, coworld_id=COWORLD)] == ["V-7 merged"]

    # The queued admission reaches the chat through the ordinary dispatcher.
    dispatch_inputs(ctx)
    assert ctx.wstore.one(
        "SELECT status FROM client_admissions WHERE op_id=?", (admission["op_id"],)
    )["status"] in ("running", "completed")


def test_a_paused_workspace_holds_updates_until_it_resumes(tmp_path):
    ctx = make_ctx(tmp_path)
    chat(ctx)
    set_setting(ctx, "paused", True)
    owner = coordinator(ctx)
    tell(ctx, "T-3 finished")
    advance(ctx, 900)
    assert "chat_updates" not in owner.advance(ctx)
    assert followups(ctx) == [] and len(chat_updates.pending(ctx.wstore, COWORLD)) == 1
    set_setting(ctx, "paused", False)
    assert owner.advance(ctx)["chat_updates"]["delivered"]["updates"] == 1


def test_without_a_chat_the_coordinator_writes_notices(tmp_path):
    ctx = make_ctx(tmp_path)
    owner = coordinator(ctx)
    tell(ctx, "T-3 finished")
    advance(ctx, 120)
    assert owner.advance(ctx)["chat_updates"] == {"noticed": 1}
    assert [n["text"] for n in notices.unread(ctx.wstore, coworld_id=COWORLD)] == ["T-3 finished"]
    assert followups(ctx) == []


def test_a_restarted_coordinator_does_not_deliver_again(tmp_path):
    ctx = make_ctx(tmp_path)
    chat(ctx)
    tell(ctx, "T-3 finished")
    advance(ctx, 120)
    coordinator(ctx).advance(ctx)
    restarted = coordinator(ctx)
    assert restarted.advance(ctx)["chat_updates"] == {}
    assert len(followups(ctx)) == 1


def test_a_queued_update_holds_the_next_batch(tmp_path):
    ctx = make_ctx(tmp_path)
    chat(ctx)
    owner = coordinator(ctx)
    tell(ctx, "first")
    advance(ctx, 120)
    owner.advance(ctx)
    tell(ctx, "second")
    advance(ctx, 600)
    report = owner.advance(ctx)["chat_updates"]
    assert report == {"waiting": {"session": "chat1", "updates": 1}}
    assert len(followups(ctx)) == 1
