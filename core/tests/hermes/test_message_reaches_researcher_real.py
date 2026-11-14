"""``task.message`` reaches a running Researcher through the watcher, on a real Hermes gateway.

A directive does not reach a Researcher already running; the Orchestrator's ``task.message`` does.
Here the message goes through the real tool, the coordinator's delivery and dispatcher, and the
gateway's steer, and the test reads what the model was sent next, both when the run goes on to a
tool call and when the model was writing what would have been its last reply. It needs no model
key. See the docstring of ``test_frozen_sessions_real.py`` for how to run it in the pinned image.
"""

from __future__ import annotations

import json
import threading
import time

from research_core.store import sessions, tasks
from research_core.store.actors import Actor
from research_core.watcher.hermes_api import HermesRuns
from tests.hermes.test_frozen_sessions_real import ROLES, FakeModel, Workspace
from tests.scenarios.test_retry_once import call_applied
from tests.test_coordinator_transitions import assigned
from tests.watcher._support import coordinator, dispatch_inputs, make_ctx

MESSAGE = "NEVER-TOUCH-THE-OPENING-BOOK"
SETTLE_S = 60


class HeldModel(FakeModel):
    """Holds the Session's first request until ``release`` is set, so a steer lands during it."""

    def __init__(self, first_reply: dict) -> None:
        self.release = threading.Event()
        self.first_seen = threading.Event()
        self.first_reply = first_reply
        self.first_done = False
        super().__init__()

    def reply(self, request: dict) -> dict:
        if not request.get("tools"):
            return super().reply(request)  # Hermes's title request
        if self.first_done:
            return super().reply(request)
        self.first_done = True
        self.first_seen.set()
        assert self.release.wait(120)
        return self.first_reply


def tool_call_reply() -> dict:
    arguments = json.dumps({"action": "read", "key": "paused"})
    function = {"name": "settings", "arguments": arguments}
    call = {"index": 0, "id": "call_1", "type": "function", "function": function}
    return {"role": "assistant", "content": "", "tool_calls": [call]}


def run_scenario(tmp_path, hermes_bin, monkeypatch, first_reply: dict):
    """Send the message while the run's first model call is out; returns everything to judge by."""
    model = HeldModel(first_reply)
    workspace = Workspace(tmp_path, hermes_bin, monkeypatch, model)
    host = workspace.host
    ctx = make_ctx(tmp_path)
    sessions.register(
        ctx.wstore,
        Actor.harness(),
        session_id="planner",
        profile="orchestrator",
        kind="wake",
        coworld_id=ctx.coworld.coworld_id,
    )
    host.workspace = ctx.ws.root
    host.write_host_dotenv()
    workspace.publish()
    workspace.boot()
    try:
        ctx.hermes = HermesRuns(
            base_url=host.base_url, keys=dict.fromkeys(ROLES, host.key), default_profile=ROLES[0]
        )
        row = assigned(ctx)
        assert model.first_seen.wait(120)
        sent = call_applied(ctx, {"action": "message", "task": "T-1", "text": MESSAGE})
        assert sent["ok"] is True, sent
        coordinator(ctx).deliver_worker_messages(ctx)
        dispatch_inputs(ctx)
        dispatch_inputs(ctx)
        model.release.set()
        deadline = time.monotonic() + SETTLE_S
        status = ctx.hermes.status(row["run_id"], profile="researcher")
        while not status.terminal and time.monotonic() < deadline:
            time.sleep(0.2)
            status = ctx.hermes.status(row["run_id"], profile="researcher")
        assert status.terminal, status
        return ctx, model, status
    finally:
        model.release.set()
        host.stop_gateway()


def model_saw_message(model: FakeModel) -> bool:
    return any(MESSAGE in json.dumps(request["messages"]) for request in model.requests)


def test_the_message_arrives_with_the_next_tool_result(tmp_path, hermes_bin, monkeypatch):
    ctx, model, status = run_scenario(tmp_path, hermes_bin, monkeypatch, tool_call_reply())
    assert status.status == "completed", status
    assert model_saw_message(model)
    assert not tasks.pending_messages(ctx.store, 1, "to_researcher")


def test_the_message_arrives_when_the_model_was_writing_its_last_reply(
    tmp_path, hermes_bin, monkeypatch
):
    final_reply = {"role": "assistant", "content": "All done."}
    ctx, model, status = run_scenario(tmp_path, hermes_bin, monkeypatch, final_reply)
    assert status.status == "completed", status
    assert not status.raw.get("pending_steer")
    assert model_saw_message(model)
    assert not tasks.pending_messages(ctx.store, 1, "to_researcher")
