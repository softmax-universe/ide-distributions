"""The ``chat_update`` tool: a wake's way to tell the user something."""

from __future__ import annotations

import pytest

import research_core.tools.chat_update  # noqa: F401  (registers the tool)
from research_core.store import chat_updates, notices, sessions
from research_core.store.actors import Actor


@pytest.fixture
def register_session(ctx_factory):
    def register(session_id, kind, coworld_id="cow_fake"):
        ctx = ctx_factory()
        return sessions.register(
            ctx.wstore,
            Actor.harness(),
            coworld_id=coworld_id,
            session_id=session_id,
            profile="orchestrator",
            kind=kind,
        )

    return register


def unread(ctx_factory, coworld_id="cow_fake"):
    return [n["text"] for n in notices.unread(ctx_factory().wstore, coworld_id=coworld_id)]


def test_researcher_has_no_chat_update(call):
    result = call("chat_update", {"category": "progress", "text": "hi"}, role="researcher")
    assert result["ok"] is False and "unknown tool 'chat_update'" in result["error"]


@pytest.mark.parametrize("category", ["outcome", "progress"])
def test_refused_in_a_chat_and_outside_a_wake(call, ctx_factory, register_session, category):
    register_session("s1", "chat")
    args = {"category": category, "text": "T-3 finished"}
    in_chat = call("chat_update", args, session_id="s1")
    assert in_chat["ok"] is False and "you are in the chat; reply here" in in_chat["error"]
    unknown = call("chat_update", args, session_id="nobody")
    assert unknown["ok"] is False and "not one" in unknown["error"]
    assert chat_updates.pending(ctx_factory().wstore, "cow_fake") == []
    assert unread(ctx_factory) == []


def test_a_wake_records_an_update_for_the_users_chat(call, ctx_factory, register_session):
    register_session("w1", "wake")
    result = call(
        "chat_update",
        {"category": "decision", "text": "Submit V-7?", "refs": ["V-7"]},
        session_id="w1",
    )
    assert result["ok"] is True and "most recent chat" in result["next"]
    assert result["update"]["category"] == "decision"
    assert result["update"]["created_by"] == "session:w1"
    [pending] = chat_updates.pending(ctx_factory().wstore, "cow_fake")
    assert (pending["text"], pending["refs"]) == ("Submit V-7?", ["V-7"])
    assert unread(ctx_factory) == []

    missing = call("chat_update", {"text": "no category"}, session_id="w1")
    assert missing["ok"] is False and "category" in missing["error"]
    unknown = call("chat_update", {"category": "news", "text": "x"}, session_id="w1")
    assert unknown["ok"] is False
    long = call("chat_update", {"category": "outcome", "text": "x" * 4001}, session_id="w1")
    assert long["ok"] is False and "at most 4000" in long["error"]


def test_progress_is_a_notice_at_once_and_says_so(call, ctx_factory, register_session):
    register_session("w1", "wake")
    result = call(
        "chat_update",
        {"category": "progress", "text": "Smoke test passed", "refs": ["T-3"]},
        session_id="w1",
    )
    assert result["ok"] is True and result["notice"]["id"] == "N-1"
    assert "not a chat message" in result["next"] and "progress" in result["next"]
    wstore = ctx_factory().wstore
    assert wstore.one("SELECT COUNT(*) FROM chat_updates")[0] == 0
    assert unread(ctx_factory) == ["Smoke test passed"]


def test_progress_from_a_workspace_wake_is_a_workspace_notice(call, ctx_factory, register_session):
    register_session("w0", "wake", coworld_id=None)
    result = call("chat_update", {"category": "progress", "text": "All quiet"}, session_id="w0")
    assert result["ok"] is True and result["notice"]["coworld_id"] is None
    assert unread(ctx_factory, coworld_id=None) == ["All quiet"]


def test_a_category_left_out_of_the_setting_is_a_notice_and_says_so(
    call, ctx_factory, register_session
):
    register_session("w1", "wake")
    result = call(
        "chat_update",
        {"category": "merge", "text": "V-7 merged", "refs": ["V-7"]},
        session_id="w1",
    )
    assert result["ok"] is True and result["notice"]["id"] == "N-1"
    assert "not a chat message" in result["next"] and "chat_updates.categories" in result["next"]
    assert chat_updates.pending(ctx_factory().wstore, "cow_fake") == []
    assert unread(ctx_factory) == ["V-7 merged"]
