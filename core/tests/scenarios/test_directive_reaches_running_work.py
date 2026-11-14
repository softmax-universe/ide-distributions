"""What the tools say about a new directive is what happens to the work.

``directive.add`` names the Tasks in progress because their Researchers do not see it; only
``task.message`` reaches them: at once in a live run, when its wait resolves if the run has ended.
A Task started later has it in its briefing. ``test_message_reaches_researcher_real.py`` checks the
last step on a real gateway.
"""

import json

import pytest

from research_core.plugin import briefing
from research_core.store import sessions, tasks
from research_core.store.actors import Actor
from research_core.tools.base import ToolContext, run_tool
from tests.scenarios.test_retry_once import call_applied
from tests.test_coordinator_transitions import assigned
from tests.watcher._support import (
    HARNESS,
    claim,
    coordinator,
    dispatch_inputs,
    make_ctx,
    make_task,
)

TEXT = "never touch the opening book"


@pytest.fixture
def ctx(tmp_path):
    context = make_ctx(tmp_path)
    sessions.register(
        context.wstore,
        HARNESS,
        session_id="planner",
        profile="orchestrator",
        kind="wake",
        coworld_id=context.coworld.coworld_id,
    )
    return context


def add_directive(ctx) -> dict:
    tool_ctx = ToolContext.from_env(
        "orchestrator",
        {
            "IDE_WORKSPACE": str(ctx.ws.root),
            "IDE_COWORLD_ID": ctx.coworld.coworld_id,
            "IDE_SESSION_ID": "planner",
        },
    )
    args = {"action": "add", "text": TEXT, "scope": "workspace", "source": "m-1"}
    try:
        return json.loads(run_tool("directive", args, tool_ctx))
    finally:
        tool_ctx.store.close()
        tool_ctx.wstore.close()


def send_message(ctx) -> None:
    sent = call_applied(ctx, {"action": "message", "task": "T-1", "text": TEXT})
    assert sent["ok"] is True, sent


def delivered_text(ctx, run_id) -> str:
    return "\n".join(ctx.hermes.steered[run_id])


def test_a_running_researcher_gets_the_directive_through_task_message(ctx):
    row = assigned(ctx)
    claim(ctx, row["session_id"], 1)

    added = add_directive(ctx)
    assert "T-1 in progress" in added["next"] and "task.message" in added["next"]
    owner = coordinator(ctx)
    owner.deliver_worker_messages(ctx)
    dispatch_inputs(ctx)
    assert row["run_id"] not in ctx.hermes.steered  # the directive alone reaches nobody

    send_message(ctx)
    owner.deliver_worker_messages(ctx)
    dispatch_inputs(ctx)
    dispatch_inputs(ctx)
    assert TEXT in delivered_text(ctx, row["run_id"])
    assert not tasks.pending_messages(ctx.store, 1, "to_researcher")


def test_a_researcher_on_a_wait_gets_it_when_the_wait_resolves(ctx):
    row = assigned(ctx)
    claim(ctx, row["session_id"], 1)
    tasks.wait(ctx.store, Actor.session(row["session_id"]), 1, kind="user", detail="an answer")
    ctx.hermes.complete(row["run_id"])
    owner = coordinator(ctx)
    owner.reconcile()

    send_message(ctx)
    report = owner.deliver_worker_messages(ctx)
    assert report["held"][0]["reason"] == "delivered when the user wait resolves"
    assert tasks.pending_messages(ctx.store, 1, "to_researcher")

    tasks.resolve_wait(ctx.store, HARNESS, 1, note="answered")
    owner.advance_events(ctx)
    [resume] = ctx.wstore.q("SELECT * FROM client_admissions WHERE native_key LIKE 'event:%'")
    assert TEXT in json.loads(resume["payload"])["input"]


def test_a_task_started_later_has_it_in_its_briefing(ctx):
    make_task(ctx, type="maintenance")
    add_directive(ctx)
    sessions.register(
        ctx.wstore,
        HARNESS,
        session_id="later",
        profile="researcher",
        kind="task",
        task_id=1,
        coworld_id=ctx.coworld.coworld_id,
    )
    tool_ctx = ToolContext.from_env(
        "researcher",
        {
            "IDE_WORKSPACE": str(ctx.ws.root),
            "IDE_COWORLD_ID": ctx.coworld.coworld_id,
            "IDE_SESSION_ID": "later",
        },
    )
    try:
        assert TEXT in briefing.render(tool_ctx, "later")
    finally:
        tool_ctx.store.close()
        tool_ctx.wstore.close()
