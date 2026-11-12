"""The ``ui`` tool: show, panel_set, notify and ask, and the conversation key (A3.5)."""

from __future__ import annotations

import json

import pytest

import research_core.tools.ui  # noqa: F401  (registers the tool)
from research_core.store import chat_updates, notices, sessions, ui_state
from research_core.store.actors import Actor
from research_core.tools.base import run_tool
from research_core.tools.ui import PANEL_KINDS

ANSWER_NOTE = "the answer arrives as a UI action; if no client can answer, ask in chat"


@pytest.fixture
def register_session(ctx_factory):
    def register(session_id, kind):
        ctx = ctx_factory()
        return sessions.register(
            ctx.wstore,
            Actor.harness(),
            coworld_id="cow_fake",
            session_id=session_id,
            profile="orchestrator",
            kind=kind,
        )

    return register


def doc(ctx_factory, conversation):
    return ui_state.get(ctx_factory().wstore, conversation)["doc"]


def test_show_writes_under_the_chat_session(call, ctx_factory, register_session):
    register_session("s1", "chat")
    result = call("ui", {"action": "show", "ref": "T-3"}, session_id="s1")
    assert result["ok"] is True
    assert result["conversation"] == "s1"
    assert result["show"]["ref"] == "T-3" and result["show"]["at"]
    assert doc(ctx_factory, "s1")["show"]["ref"] == "T-3"
    assert "show" not in doc(ctx_factory, "workspace")

    viewed = call("ui", {"action": "show", "view": "leaderboard"}, session_id="s1")
    assert viewed["show"] == {"view": "leaderboard", "at": viewed["show"]["at"]}
    assert doc(ctx_factory, "s1")["show"] == viewed["show"]


def test_wake_and_unregistered_sessions_write_under_workspace(call, ctx_factory, register_session):
    register_session("w1", "wake")
    waked = call("ui", {"action": "show", "ref": "G-1"}, session_id="w1")
    assert waked["conversation"] == "coworld:cow_fake"
    unknown = call("ui", {"action": "show", "ref": "G-2"}, session_id="nobody")
    assert unknown["conversation"] == "workspace"
    assert doc(ctx_factory, "workspace")["show"]["ref"] == "G-2"
    assert ui_state.get(ctx_factory().wstore, "w1")["version"] == 0


def test_a_wake_is_not_told_to_ask_in_chat(call, register_session):
    register_session("w1", "wake")
    asked = call("ui", {"action": "ask", "question": "Submit V-7?"}, session_id="w1")
    assert asked["ok"] is True and "in chat" not in asked["next"] and "end the run" in asked["next"]


def test_parallel_wakes_do_not_ask_the_same_question_twice(call, ctx_factory, register_session):
    register_session("w1", "wake")
    register_session("w2", "wake")
    first = call(
        "ui", {"action": "ask", "question": "Submit V-2?", "refs": ["V-2"]}, session_id="w1"
    )
    second = call(
        "ui",
        {"action": "ask", "question": "Shall I submit V-2 now?", "refs": ["V-2"]},
        session_id="w2",
    )
    assert second["ok"] is True and second["already_open"] is True
    assert second["ask_id"] == first["ask_id"] and "already waiting" in second["next"]
    other = call(
        "ui", {"action": "ask", "question": "Submit V-3?", "refs": ["V-3"]}, session_id="w2"
    )
    assert other["ask_id"] != first["ask_id"]
    assert set(doc(ctx_factory, "coworld:cow_fake")["asks"]) == {first["ask_id"], other["ask_id"]}


def test_show_needs_exactly_one_of_ref_or_view(call):
    neither = call("ui", {"action": "show"})
    assert neither["ok"] is False and "ref" in neither["error"] and "view" in neither["error"]
    both = call("ui", {"action": "show", "ref": "T-1", "view": "v"})
    assert both["ok"] is False
    garbage = call("ui", {"action": "show", "ref": "the thing"})
    assert garbage["ok"] is False and "reference" in garbage["error"]


def test_panel_set_creates_the_container_and_keeps_other_panels(call, ctx_factory):
    assert set(PANEL_KINDS) >= {"markdown", "file", "agent-context"}
    first = call(
        "ui", {"action": "panel_set", "panel": "notes", "kind": "markdown", "content": "# Hi"}
    )
    assert first["ok"] is True and first["panel"] == "notes"
    assert first["conversation"] == "workspace"
    second = call(
        "ui",
        {"action": "panel_set", "panel": "table", "kind": "table", "content": {"rows": [[1]]}},
    )
    assert second["ok"] is True
    panels = doc(ctx_factory, "workspace")["panels"]
    assert panels["notes"]["kind"] == "markdown" and panels["notes"]["content"] == "# Hi"
    assert panels["table"]["content"] == {"rows": [[1]]}
    assert panels["notes"]["at"] and panels["table"]["at"]
    replaced = call(
        "ui", {"action": "panel_set", "panel": "notes", "kind": "markdown", "content": "# Bye"}
    )
    assert replaced["ok"] is True
    assert doc(ctx_factory, "workspace")["panels"]["notes"]["content"] == "# Bye"

    bad_kind = call("ui", {"action": "panel_set", "panel": "x", "kind": "pdf", "content": "a"})
    assert bad_kind["ok"] is False and "kind must be one of" in bad_kind["error"]


def test_file_panels_must_point_under_the_documents_folder(call, ctx_factory, tmp_path):
    documents = ctx_factory().coworld().documents
    (documents / "report.md").write_text("# Report\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("no\n")

    relative = call(
        "ui", {"action": "panel_set", "panel": "doc", "kind": "file", "content": "report.md"}
    )
    assert relative["ok"] is True
    stored = doc(ctx_factory, "workspace")["panels"]["doc"]["content"]
    assert stored == str((documents / "report.md").resolve())

    absolute = call(
        "ui",
        {
            "action": "panel_set",
            "panel": "doc",
            "kind": "file",
            "content": str(documents / "report.md"),
        },
    )
    assert absolute["ok"] is True

    refused = call(
        "ui", {"action": "panel_set", "panel": "doc", "kind": "file", "content": str(outside)}
    )
    assert refused["ok"] is False and "documents" in refused["error"]
    escaped = call(
        "ui", {"action": "panel_set", "panel": "doc", "kind": "file", "content": "../secret.txt"}
    )
    assert escaped["ok"] is False
    missing = call(
        "ui", {"action": "panel_set", "panel": "doc", "kind": "file", "content": "nope.md"}
    )
    assert missing["ok"] is False and "does not exist" in missing["error"]
    assert ".state/" not in str(refused) + str(escaped) + str(missing)


def test_notify_writes_an_unread_notice_with_no_client_attached(call, ctx_factory):
    result = call(
        "ui",
        {"action": "notify", "text": "T-3 finished", "refs": ["T-3"], "urgency": "high"},
    )
    assert result["ok"] is True
    assert result["record"]["id"] == "N-1" and result["record"]["urgency"] == "high"
    wstore = ctx_factory().wstore
    unread = notices.unread(wstore, coworld_id="cow_fake")
    assert [n["text"] for n in unread] == ["T-3 finished"]
    assert unread[0]["refs"] == ["T-3"]
    kinds = [e.kind for e in wstore.events_after(0)]
    assert kinds.count("notice") == 1 and "ui.patch" not in kinds

    plain = call("ui", {"action": "notify", "text": "all quiet"})
    assert plain["record"]["urgency"] == "normal" and plain["record"]["refs"] == []
    bad = call("ui", {"action": "notify", "text": "x", "urgency": "loud"})
    assert bad["ok"] is False


def test_ask_opens_a_question_and_says_how_the_answer_arrives(call, ctx_factory, register_session):
    register_session("s1", "chat")
    result = call(
        "ui",
        {
            "action": "ask",
            "question": "Submit V-7 now?",
            "options": ["yes", "no"],
            "refs": ["V-7"],
        },
        session_id="s1",
    )
    assert result["ok"] is True
    ask_id = result["ask_id"]
    assert ask_id and result["conversation"] == "s1"
    assert result["next"] == ANSWER_NOTE
    asks = doc(ctx_factory, "s1")["asks"]
    assert asks[ask_id]["status"] == "open"
    assert asks[ask_id]["question"] == "Submit V-7 now?"
    assert asks[ask_id]["options"] == ["yes", "no"] and asks[ask_id]["refs"] == ["V-7"]

    again = call("ui", {"action": "ask", "question": "And V-8?"}, session_id="s1")
    assert again["ask_id"] != ask_id
    assert set(doc(ctx_factory, "s1")["asks"]) == {ask_id, again["ask_id"]}

    bad = call("ui", {"action": "ask", "question": "x", "options": [1, 2]})
    assert bad["ok"] is False


def test_researcher_cannot_use_ui(call):
    result = call("ui", {"action": "notify", "text": "hi"}, role="researcher")
    assert result["ok"] is False and "unknown tool 'ui'" in result["error"]


def test_user_identity_notice_uses_bound_game(ctx_factory, register_session):
    register_session("s1", "chat")
    ctx = ctx_factory(session_id="s1")
    ctx.actor = Actor.user("fixture-user")
    result = json.loads(run_tool("ui", {"action": "notify", "text": "Game news"}, ctx))
    assert result["ok"] is True
    assert [n["text"] for n in notices.unread(ctx.wstore, coworld_id="cow_fake")] == ["Game news"]
    assert notices.unread(ctx.wstore) == []


def test_a_chats_needs_join_the_shared_panel_and_keep_the_computed_part(
    call, ctx_factory, register_session
):
    register_session("s1", "chat")
    ctx = ctx_factory(session_id="s1")
    ui_state.patch(
        ctx.wstore,
        Actor.harness(),
        "coworld:cow_fake",
        [
            {
                "op": "add",
                "path": "/panels",
                "value": {
                    "agent-context": {
                        "kind": "agent-context",
                        "content": {"computed": {"paused": False}, "needs": []},
                    }
                },
            }
        ],
    )
    args = {"action": "panel_set", "panel": "agent-context", "kind": "agent-context"}
    needs = {"needs": ["A User credential, to read your other Players"]}
    result = call("ui", args | {"content": needs}, session_id="s1")
    assert result["ok"] is True and result["conversation"] == "coworld:cow_fake"
    content = doc(ctx_factory, "coworld:cow_fake")["panels"]["agent-context"]["content"]
    assert content == {"computed": {"paused": False}, **needs}
    assert "panels" not in doc(ctx_factory, "s1")

    refused = call("ui", args | {"content": {"goals": []}}, session_id="s1")
    assert refused["ok"] is False and "needs" in refused["error"]


def test_tell_is_refused_in_a_chat_and_outside_a_wake(call, ctx_factory, register_session):
    register_session("s1", "chat")
    args = {"action": "tell", "category": "outcome", "text": "T-3 finished"}
    in_chat = call("ui", args, session_id="s1")
    assert in_chat["ok"] is False and "you are in the chat; reply here" in in_chat["error"]
    unknown = call("ui", args, session_id="nobody")
    assert unknown["ok"] is False and "not one" in unknown["error"]
    assert chat_updates.pending(ctx_factory().wstore, "cow_fake") == []


def test_a_wake_tells_the_users_chat(call, ctx_factory, register_session):
    register_session("w1", "wake")
    result = call(
        "ui",
        {"action": "tell", "category": "decision", "text": "Submit V-7?", "refs": ["V-7"]},
        session_id="w1",
    )
    assert result["ok"] is True and "most recent chat" in result["next"]
    assert result["update"]["category"] == "decision"
    assert result["update"]["created_by"] == "session:w1"
    [pending] = chat_updates.pending(ctx_factory().wstore, "cow_fake")
    assert (pending["text"], pending["refs"]) == ("Submit V-7?", ["V-7"])

    missing = call("ui", {"action": "tell", "text": "no category"}, session_id="w1")
    assert missing["ok"] is False and "category" in missing["error"]
    long = call(
        "ui", {"action": "tell", "category": "outcome", "text": "x" * 4001}, session_id="w1"
    )
    assert long["ok"] is False and "at most 4000" in long["error"]


def test_a_category_left_out_of_the_setting_is_a_notice_and_says_so(
    call, ctx_factory, register_session
):
    register_session("w1", "wake")
    result = call(
        "ui",
        {"action": "tell", "category": "merge", "text": "V-7 merged", "refs": ["V-7"]},
        session_id="w1",
    )
    assert result["ok"] is True and result["notice"]["id"] == "N-1"
    assert "not a chat message" in result["next"] and "merge" in result["next"]
    wstore = ctx_factory().wstore
    assert chat_updates.pending(wstore, "cow_fake") == []
    assert [n["text"] for n in notices.unread(wstore, coworld_id="cow_fake")] == ["V-7 merged"]
