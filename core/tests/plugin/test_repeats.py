"""The repeat hooks: a firmer refusal on the second identical one, a notice on a loop-guard halt."""

from __future__ import annotations

import json

import pytest

from research_core import layout
from research_core.plugin import repeats
from research_core.store import sessions
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.tools.base import argument_error
from research_core.tools.catalog import TOOLS, _load_tool_modules

HARNESS = Actor.harness()
CLAIM_ARGS = {"action": "write", "text": "the champion is pv_1"}


@pytest.fixture(autouse=True)
def fresh_counts(monkeypatch):
    """The hooks keep per-Session counts in module state; each test starts without any."""
    monkeypatch.setattr(repeats, "_refusals", {})
    monkeypatch.setattr(repeats, "_failures", {})


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("IDE_WORKSPACE", str(tmp_path / "ws"))
    ws = layout.from_env({"IDE_WORKSPACE": str(tmp_path / "ws")})
    ws.ensure_dirs()
    return ws


def claim_refusal() -> str:
    """The real refusal of a ``claim write`` that leaves out its required fields."""
    _load_tool_modules()
    spec = TOOLS["claim"]
    payload = argument_error(
        spec,
        "orchestrator",
        "write",
        CLAIM_ARGS,
        ["summary is required for write", "tags is required for write"],
    )
    return json.dumps(payload)


def test_second_identical_refusal_is_replaced_and_the_first_and_successes_are_not():
    hook = repeats.make_hooks("orchestrator")["transform_tool_result"]
    refusal = claim_refusal()

    def call(result, args=CLAIM_ARGS, session_id="s-firm"):
        return hook(tool_name="claim", args=args, result=result, session_id=session_id)

    assert call(refusal) is None, "the first refusal keeps its full text and usage"
    second = json.loads(call(refusal))
    assert second["ok"] is False
    assert second["error"] == (
        "This exact call was refused 2 times. Supply summary, tags, or report the problem to the "
        "user (from a wake, a failure chat_update) and end the run."
    )
    assert "usage" not in second and len(json.dumps(second)) < len(refusal)
    assert '"error"' in json.dumps(second)[:500], "Hermes's guard still counts it as a failure"
    assert "refused 3 times" in json.loads(call(refusal))["error"]

    assert call('{"ok": true, "id": "C-1"}') is None, "a success is left alone"
    assert call(refusal) is None, "a success resets the count for that call"
    assert call(refusal, args={**CLAIM_ARGS, "text": "other"}) is None, "other arguments"
    assert call(refusal, session_id="s-other") is None, "other Session"

    end = repeats.make_hooks("orchestrator")["on_session_end"]
    end(session_id="s-firm", turn_exit_reason="text_response(stop)")
    assert call(refusal) is None, "counts start over each turn, as Hermes's do"


def test_researcher_refusal_points_at_the_orchestrator():
    hook = repeats.make_hooks("researcher")["transform_tool_result"]
    refusal = json.dumps({"ok": False, "error": "x", "problems": ["unknown field 'y'"]})
    hook(tool_name="goal", args={"y": 1}, result=refusal, session_id="s-r")
    text = json.loads(hook(tool_name="goal", args={"y": 1}, result=refusal, session_id="s-r"))
    assert text["error"] == (
        "This exact call was refused 2 times. Fix what the refusal names (unknown field 'y'), "
        "or report the problem with task_ask if you hold a Task and end the run."
    )


def test_transform_never_raises_and_ignores_non_refusals():
    hook = repeats.make_hooks("orchestrator")["transform_tool_result"]
    assert hook(tool_name="claim", args=None, result=None, session_id="s") is None
    assert (
        hook(tool_name="terminal", args={"command": "x"}, result="Exit code: 1", session_id="s")
        is None
    )
    assert (
        hook(tool_name="terminal", args={"command": "x"}, result="Exit code: 1", session_id="s")
        is None
    )
    assert hook(tool_name="claim", args=CLAIM_ARGS, result=claim_refusal()) is None  # no Session
    assert hook(tool_name="claim", args=object(), result="{", session_id="s") is None


def test_a_short_refusal_keeps_what_the_dropped_copy_held():
    conflict = {
        "ok": False,
        "error": "the page changed since you read it",
        "problems": ["wiki edit conflict"],
        "conflict": True,
        "current_body": "the page now",
        "current_revision_id": "r2",
        "context": {"action": "edit"},
        "usage": {"does": "long"},
    }
    short = json.loads(repeats.firmer_refusal("researcher", "wiki_edit", conflict, 2))
    assert short["current_body"] == "the page now"
    assert short["current_revision_id"] == "r2"
    assert short["context"] == {"action": "edit", "tool": "wiki_edit", "refused": 2}
    assert "usage" not in short


def refused_pair(call_id: str, arguments: str = '{"action":"write"}', name: str = "claim") -> list:
    """A call and its refusal as they appear in a request, with Hermes's loop warning appended."""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": claim_refusal() + "\n\n[Tool loop warning: ...]",
        },
    ]


def test_a_request_keeps_only_the_latest_of_identical_refused_calls():
    user = {"role": "user", "content": "record it"}
    first, second, third = refused_pair("a"), refused_pair("b"), refused_pair("c")
    other = refused_pair("d", arguments='{"action":"write","summary":"x"}')
    request = {"model": "m", "messages": [user, *first, *other, *second, *third]}

    rewritten = repeats.without_repeated_refusals(request=request, provider="openrouter")["request"]

    assert rewritten["messages"] == [user, *other, *third]
    assert rewritten["model"] == "m"
    assert len(request["messages"]) == 9, "the caller's request is not edited in place"


def test_results_carrying_hermes_cache_markers_are_read():
    """On OpenRouter with Kimi or Claude, Hermes sends the latest results as marked text parts."""

    def marked(pair: list) -> list:
        text_part = {
            "type": "text",
            "text": pair[1]["content"],
            "cache_control": {"type": "ephemeral"},
        }
        return [pair[0], {**pair[1], "content": [text_part]}]

    user = {"role": "user", "content": "record it"}
    first, second, third = refused_pair("a"), marked(refused_pair("b")), marked(refused_pair("c"))
    rewritten = repeats.without_repeated_refusals(
        request={"messages": [user, *first, *second, *third]}
    )
    assert rewritten["request"]["messages"] == [user, *third]


def test_a_result_holding_more_than_loop_guard_notes_is_kept():
    user = {"role": "user", "content": "record it"}
    steered = refused_pair("b")
    steered[1] = {
        **steered[1],
        "content": steered[1]["content"] + "\n\n[OUT-OF-BAND USER MESSAGE]\nUse L-8 instead.\n"
        "[/OUT-OF-BAND USER MESSAGE]",
    }
    first, third = refused_pair("a"), refused_pair("c")
    guarded = refused_pair("d")
    guarded[1] = {**guarded[1], "content": guarded[1]["content"] + "\n\n[hermes note: stop.]"}
    rewritten = repeats.without_repeated_refusals(
        request={"messages": [user, *first, *steered, *third, *guarded]}
    )
    assert rewritten["request"]["messages"] == [user, *steered, *guarded]


def test_a_result_holding_an_image_is_kept():
    first, second = refused_pair("a"), refused_pair("b")
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}
    first[1] = {**first[1], "content": [{"type": "text", "text": first[1]["content"]}, image]}
    assert repeats.without_repeated_refusals(request={"messages": [*first, *second]}) is None


def test_a_drop_never_leaves_two_user_messages_in_a_row():
    first_user, second_user = {"role": "user", "content": "a"}, {"role": "user", "content": "b"}
    first, second = refused_pair("a"), refused_pair("b")
    rewritten = repeats.without_repeated_refusals(
        request={"messages": [first_user, *first, second_user, *second]}
    )
    assert rewritten is None, "nothing could be dropped"
    third = refused_pair("c")
    rewritten = repeats.without_repeated_refusals(
        request={"messages": [first_user, *first, *second, second_user, *third]}
    )
    assert rewritten["request"]["messages"] == [first_user, *second, second_user, *third]


def test_a_request_without_repeats_is_left_alone():
    success = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "a", "function": {"name": "claim", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "a", "content": '{"ok": true}'},
    ]
    two_calls = {
        "role": "assistant",
        "tool_calls": [
            {"id": "b", "function": {"name": "claim", "arguments": "{}"}},
            {"id": "c", "function": {"name": "claim", "arguments": "{}"}},
        ],
    }
    for messages in (
        [],
        refused_pair("a"),
        [*success, *success],  # identical calls that worked
        [*refused_pair("a"), *refused_pair("b", name="task")],  # another tool
        [two_calls, *refused_pair("d")[1:], two_calls, *refused_pair("e")[1:]],  # a parallel batch
        refused_pair("a")[:1],  # no result yet
    ):
        assert repeats.without_repeated_refusals(request={"messages": messages}) is None
    assert repeats.without_repeated_refusals(request={"input": "a Responses request"}) is None


def test_the_middleware_never_raises():
    broken = {"messages": [{"role": "assistant", "tool_calls": [{"id": "a"}]}, {"content": "{}"}]}
    assert repeats.without_repeated_refusals(request=broken) is None
    assert repeats.without_repeated_refusals(request={"messages": "not a list"}) is None
    parts = {"role": "tool", "tool_call_id": "a", "content": ["not a text part"]}
    broken = {"messages": [refused_pair("a")[0], parts]}
    assert repeats.without_repeated_refusals(request=broken) is None


def test_halt_writes_one_notice_naming_the_session_kind_and_tool(workspace):
    wstore = Store.open(workspace.store_path, kind="workspace")
    sessions.register(
        wstore,
        HARNESS,
        session_id="s-wake",
        profile="orchestrator",
        kind="wake",
        event="champion.changed",
    )
    hooks = repeats.make_hooks("orchestrator")
    refusal = claim_refusal()
    for _ in range(5):
        hooks["transform_tool_result"](
            tool_name="claim", args=CLAIM_ARGS, result=refusal, session_id="s-wake"
        )
    hooks["transform_tool_result"](
        tool_name="goal",
        args={},
        result='{"ok": false, "error": "x", "problems": ["x"]}',
        session_id="s-wake",
    )
    for _ in range(9):  # terminal failures never halt in Hermes, so they never name the tool
        hooks["transform_tool_result"](
            tool_name="terminal", args={"command": "x"}, result="Exit code: 1", session_id="s-wake"
        )

    hooks["on_session_end"](session_id="s-wake", turn_exit_reason="guardrail_halt", completed=True)

    [notice] = _notices(wstore)
    assert notice["text"] == (
        "Hermes's loop guard stopped the orchestrator's wake Session s-wake for champion.changed"
        " after repeated calls (most failures: claim). Its work may be unfinished."
    )
    assert notice["urgency"] == "high"


def test_halt_without_counted_failures_names_no_tool(workspace):
    # An identical-call halt: the same terminal command with the same output, never failing.
    wstore = Store.open(workspace.store_path, kind="workspace")
    hooks = repeats.make_hooks("researcher")
    for _ in range(10):
        hooks["transform_tool_result"](
            tool_name="terminal", args={"command": "x"}, result="Exit code: 1", session_id="s-r"
        )
    hooks["on_session_end"](session_id="s-r", turn_exit_reason="guardrail_halt")
    [notice] = _notices(wstore)
    assert "researcher's unregistered Session s-r after repeated calls." in notice["text"]
    assert "most failures" not in notice["text"]


def test_a_normal_end_writes_no_notice(workspace):
    wstore = Store.open(workspace.store_path, kind="workspace")
    hooks = repeats.make_hooks("orchestrator")
    hooks["transform_tool_result"](
        tool_name="claim", args=CLAIM_ARGS, result=claim_refusal(), session_id="s-chat"
    )
    for reason in ("text_response(stop)", "interrupted", None):
        hooks["on_session_end"](session_id="s-chat", turn_exit_reason=reason)
    assert _notices(wstore) == []


def test_halt_hook_never_raises_without_a_workspace(tmp_path, monkeypatch):
    not_a_folder = tmp_path / "file"
    not_a_folder.write_text("x")
    monkeypatch.setenv("IDE_WORKSPACE", str(not_a_folder))  # no store can be opened there
    end = repeats.make_hooks("researcher")["on_session_end"]
    assert end(session_id="s", turn_exit_reason="guardrail_halt") is None


def _notices(store: Store) -> list[dict]:
    return [dict(row) for row in store.q("SELECT * FROM notices ORDER BY id")]
