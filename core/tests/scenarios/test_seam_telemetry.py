"""What ``research serve``, the coordinator and short commands record, read back from the stream.

Requests reach a real seam over HTTP, as the AIDE backend sends them, on the fake gateway.
The dispatcher runs one pass at a time, so the order of records is the order of the test's steps.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from opentelemetry import trace

from research_core import attachments, layout, transitions
from research_core.agent_telemetry import observed
from research_core.agent_telemetry.rebuild import rebuild_session
from research_core.agent_telemetry.stream import STREAM_VARIABLE
from research_core.coordinator import Coordinator
from research_core.messages import UserMessage
from research_core.plugin import executor
from research_core.plugin.messages import record_native_message
from research_core.plugin.tracing import make_instrumentation
from research_core.serve import SeamServer
from research_core.store import sessions
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.tools.base import ToolContext, run_tool
from research_core.tools.catalog import TOOLS
from tests.telemetry.conftest import processed, raw_lines, stored_records
from tests.telemetry.fake_datadog import FakeDatadog
from tests.test_coordinator_transitions import assigned
from tests.test_serve_run_failures import Dispatch
from tests.watcher._support import COWORLD, coordinator, make_ctx, make_task

SECRET = "sk-seeded-0123456789abcdefghij"


class Seam:
    """A real ``research serve`` HTTP server. Nothing dispatches until the test says so."""

    def __init__(self, tmp_path: Path) -> None:
        self.ctx = make_ctx(tmp_path)
        self.hermes = self.ctx.hermes
        self.server = SeamServer(ws=self.ctx.ws, hermes=self.hermes, env=dict(self.ctx.env))
        threading.Thread(target=self.server._http.serve_forever, daemon=True).start()

    def dispatch(self) -> None:
        self.server._last_native_poll.clear()  # Each pass may read a run's outcome.
        asyncio.run(self.server.dispatch_messages())

    def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        """One request to the seam, as the AIDE backend makes it."""
        return httpx.post(f"{self.server.url}{path}", json=body, timeout=10)

    def open(self) -> str:
        opened = self.post("/session-open", _open_body(str(uuid4())))
        assert opened.status_code == 200, opened.text
        return opened.json()["session_id"]

    def records(self) -> list[dict[str, Any]]:
        processed(self.server.telemetry, 10)
        return stored_records(Path(os.environ[STREAM_VARIABLE]))


@pytest.fixture
def seam(tmp_path):
    seam = Seam(tmp_path)
    yield seam
    seam.server.shutdown()


def _open_body(op_id: str) -> dict[str, Any]:
    return {
        "op_id": op_id,
        "client": {"id": "ide", "kind": "web"},
        "actor": "user:aide",
        "session_context": {
            "contract_version": 1,
            "scope": "coworld",
            "coworld": COWORLD,
            "league": "league_1",
        },
    }


def _message_body(session_id: str, op_id: str, text: str = "Hello") -> dict[str, Any]:
    return {
        "op_id": op_id,
        "session_id": session_id,
        "message": {"role": "user", "content": text},
        "context": {"contract_version": 1},
        "actor": "user:aide",
    }


def _operations(records: Iterable[dict], **wanted: Any) -> list[dict]:
    return [
        record
        for record in records
        if record["family"] == "operation"
        and all(record.get(name) == value for name, value in wanted.items())
    ]


def _store_event(records: Iterable[dict], kind: str, **wanted: Any) -> dict:
    (record,) = _operations(records, action="store_event", native_event_type=kind, **wanted)
    return record


def _assigned(seam: Seam) -> dict[str, Any]:
    ctx = seam.ctx
    task = make_task(ctx, type="maintenance")

    class Command:
        env = ctx.env
        store = ctx.store
        wstore = ctx.wstore
        actor = Actor.harness()

        def coworld(self):
            return ctx.coworld

    return Coordinator.reserve(
        Command(),
        task_id=int(task["id"][2:]),
        task_updated=task["updated"],
        profile="researcher",
        assignment_id=task["id"],
        context=f"Assignment context {SECRET}",
        rationale=f"Rationale {SECRET}",
    )


def test_one_chat_is_one_trace_from_admission_to_turn_to_tool(seam, monkeypatch, spans):
    monkeypatch.setenv("IDE_WORKSPACE", str(seam.ctx.ws.root))
    open_op_id, message_op_id = str(uuid4()), str(uuid4())
    opening = seam.post("/session-open", _open_body(open_op_id))
    assert opening.status_code == 200, opening.text
    session_id = opening.json()["session_id"]
    message = seam.post("/message", _message_body(session_id, message_op_id))
    assert message.status_code == 200, message.text
    seam.dispatch()
    run_id = seam.ctx.wstore.one(
        "SELECT run_id FROM client_admissions WHERE op_id=?", (message_op_id,)
    )[0]

    # The gateway's side, in Hermes's order: the turn starts, its first model request binds the
    # admission's native row, and the turn's span joins the message's trace.
    hooks, traces = make_instrumentation("orchestrator")
    hooks["pre_llm_call"](session_id=session_id, task_id="task", turn_id="turn-1")
    record_native_message(
        bind_origin=traces.bind_origin,
        session_id=session_id,
        conversation_history=[{"role": "user", "_row_id": 77}],
    )
    hooks["pre_api_request"](turn_id="turn-1", api_request_id="turn-1:api:1", retry_count=0)
    # A tool call inside that turn, and the Task it leads to.
    tool_ctx = ToolContext(
        role="orchestrator",
        actor=Actor.session(session_id),
        workspace=seam.ctx.ws,
        env={
            "IDE_SESSION_ID": session_id,
            "IDE_TOOL_CALL_ID": "call_1",
            "IDE_COWORLD_ID": COWORLD,
        },
    )
    with trace.use_span(traces.session_turn(session_id).span, end_on_exit=False):
        created = json.loads(
            run_tool(
                "goal",
                {"action": "create", "title": "G", "end_state": "E", "reason": "R"},
                tool_ctx,
            )
        )
    assert created["ok"], created
    assignment = _assigned(seam)
    seam.dispatch()
    seam.hermes.complete(run_id)
    seam.dispatch()

    records = seam.records()

    # Opening: the admission ID and the Session it opened, on one record.
    (opened,) = _operations(records, action="admission", native_event_type="session_open")
    assert opened["stage"] == "accepted"
    assert opened["admission_id"] == open_op_id
    assert opened["session_id"] == session_id
    assert (opened["session_scope"], opened["coworld_id"]) == ("coworld", COWORLD)

    # Admission -> run -> Session, in order, each by its own join field.
    (admitted,) = _operations(records, action="admission", native_event_type="message")
    assert admitted["stage"] == "accepted"
    assert admitted["admission_id"] == message_op_id
    started = _store_event(records, "client.started", admission_id=message_op_id)
    assert (started["run_id"], started["session_id"]) == (run_id, session_id)
    (bound,) = _operations(records, action="admission", stage="bound")
    assert bound["admission_id"] == message_op_id
    assert bound["session_id"] == session_id
    assert bound["hermes_message_id"] == "77"
    (tool,) = _operations(records, action="workspace_tool")
    assert (tool["outcome"], tool["tool_name"]) == ("succeeded", "goal")
    assert (tool["tool_call_id"], tool["session_id"]) == ("call_1", session_id)
    goal = _store_event(records, "goal.created")
    assert (goal["goal_id"], goal["session_id"]) == (created["record"]["id"], session_id)
    # One message, one trace: its admission, the turn that served it and that turn's tool.
    turn = traces.session_turn(session_id).span.get_span_context()
    assert admitted["trace_id"] == bound["trace_id"] == f"{turn.trace_id:032x}"
    assert bound["span_id"] == f"{turn.span_id:016x}"
    assert tool["trace_id"] == goal["trace_id"] == admitted["trace_id"]
    finished = _store_event(records, "client.finished", admission_id=message_op_id)
    assert finished["outcome"] == "completed"
    chain = [opened, admitted, started, bound, goal, tool, finished]
    sequences = [record["sequence"] for record in chain]
    assert sequences == sorted(sequences), sequences

    # The Task assignment: a Session with its scope, the admission that starts it, and its run.
    task_session = assignment["session_id"]
    registered = _store_event(records, "session.started", session_id=task_session)
    assert (registered["kind"], registered["session_scope"]) == ("task", "coworld")
    assert registered["task_id"].startswith("T-")
    assigned = _store_event(records, "task.assigned")
    assert assigned["assignment_id"] == assigned["admission_id"] == assignment["op_id"]
    assert assigned["session_id"] == task_session
    worker = _store_event(records, "client.started", admission_id=assignment["op_id"])
    assert worker["session_id"] == task_session
    assert worker["run_id"] != run_id
    # The prompt, with the secret, reaches Datadog only inside the model request.
    assert SECRET not in json.dumps(records)
    assert seam.ctx.store.q("SELECT 1 FROM events WHERE kind='goal.created'")


def test_a_rolled_back_transaction_records_nothing(seam):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with seam.ctx.wstore.tx(Actor.harness()) as tx:
            tx.event("notice", "rolled-back", {})
            raise Boom
    with seam.ctx.wstore.tx(Actor.harness()) as tx:
        tx.event("drain.done", "committed", {})
    records = seam.records()
    assert [r for r in records if r.get("native_event_type") == "notice"] == []
    assert _store_event(records, "drain.done")["details"]["store_seq"] > 0


def test_one_event_that_cannot_be_recorded_costs_only_its_own_record(seam, monkeypatch):
    fields = observed._event_fields

    def fails_on_notice(store, actor, seq, kind, subject, payload):
        if kind == "notice":
            raise ValueError("cannot record this one")
        return fields(store, actor, seq, kind, subject, payload)

    monkeypatch.setattr(observed, "_event_fields", fails_on_notice)
    with seam.ctx.wstore.tx(Actor.harness()) as tx:
        tx.event("notice", "first", {})
        tx.event("drain.done", "second", {})
    records = seam.records()
    assert [r for r in records if r.get("native_event_type") == "notice"] == []
    assert _store_event(records, "drain.done")


def test_an_event_not_listed_sends_only_its_type(seam):
    with seam.ctx.wstore.tx(Actor.harness()) as tx:
        tx.event("drain.done", f"subject {SECRET}", {"text": SECRET, "count": 3})
    record = _store_event(seam.records(), "drain.done")
    assert set(record["details"]) == {"store_seq"}
    assert record["reported_actor"] == "harness"
    assert SECRET not in json.dumps(record)


def test_no_secret_reaches_a_record_from_an_exception_or_a_message(seam, monkeypatch):
    session_id = seam.open()

    def fails(*_: Any, **__: Any) -> None:
        raise attachments.AttachmentError(f"could not read {SECRET}")

    monkeypatch.setattr(attachments, "prepare", fails)
    text = f"My key is {SECRET}"
    delivered = str(uuid4())
    assert seam.post("/message", _message_body(session_id, delivered, text)).status_code == 200
    seam.dispatch()
    other_game = _message_body(session_id, str(uuid4()), text)
    other_game["context"]["coworld"] = "cow_other"
    refused = seam.post("/message", other_game)
    assert refused.status_code == 409, refused.text

    wstore = seam.ctx.wstore
    (failed,) = wstore.q("SELECT payload FROM events WHERE kind='client.delivery_failed'")
    assert SECRET in failed["payload"], "the seeded exception reached the store event"
    assert wstore.q("SELECT 1 FROM client_admissions WHERE message LIKE ?", (f"%{SECRET}%",))

    records = seam.records()
    assert SECRET not in json.dumps(records)
    (attachment,) = _operations(records, action="attachment", stage="failed")
    assert attachment["reason"] == "attachmenterror"
    assert attachment["admission_id"] == delivered
    assert _store_event(records, "client.delivery_failed", admission_id=delivered)


def test_refusals_are_recorded_and_reach_rebuild(seam):
    session_id = seam.open()

    other_game = _message_body(session_id, str(uuid4()))
    other_game["context"]["coworld"] = "cow_other"
    invalid = other_game["op_id"]
    refused_message = seam.post("/message", other_game)
    assert refused_message.status_code == 409, refused_message.text
    unknown = str(uuid4())
    cancel = seam.post("/message-cancel", {"op_id": unknown, "actor": "user:aide"})
    assert cancel.status_code == 404, cancel.text
    model = seam.post("/models", {"actor": "session:x"})
    assert model.status_code == 400, model.text

    records = seam.records()
    refused = {r["native_event_type"]: r for r in _operations(records, stage="refused")}
    assert refused["message"]["admission_id"] == invalid
    assert refused["message"]["session_id"] == session_id
    assert refused["message"]["details"] == {"status": 409}
    assert refused["message_cancel"]["admission_id"] == unknown
    assert refused["message_cancel"]["reason"] == "not_found"
    assert refused["models"]["details"] == {"status": 400}

    datadog = FakeDatadog()
    datadog.take_lines(raw_lines(Path(os.environ[STREAM_VARIABLE])))
    report = rebuild_session(datadog.search(), session_id)
    datadog.close()
    stages = [event["stage"] for event in report["events"] if event.get("admission_id") == invalid]
    assert stages == ["refused"]


def test_a_retried_request_writes_no_second_acceptance(seam):
    session_id = seam.open()
    body = _message_body(session_id, str(uuid4()))
    assert seam.post("/message", body).status_code == 200
    assert seam.post("/message", body).status_code == 200
    records = seam.records()
    accepted = _operations(records, action="admission", native_event_type="message")
    assert [r["admission_id"] for r in accepted] == [body["op_id"], body["op_id"]]
    assert _store_event(records, "client.accepted", admission_id=body["op_id"])


def test_a_held_chat_records_its_old_run_and_resumes_on_a_new_one(tmp_path):
    dispatch = Dispatch(tmp_path)
    try:
        op_id = dispatch.say("tune the policy")
        old_run = dispatch.start_and_fail(
            op_id, "HTTP 429: slow down", "rate_limit", status_code=429
        )
        held = dispatch.admission(op_id)
        dispatch.clock.now = datetime.fromisoformat(held["resume_after"]) + timedelta(seconds=1)
        dispatch.tick()
        processed(dispatch.server.telemetry, 10)
        records = stored_records(Path(os.environ[STREAM_VARIABLE]))
    finally:
        dispatch.close()
    waiting = _store_event(records, "client.waiting", admission_id=op_id)
    assert waiting["run_id"] == old_run
    assert waiting["reason"] == "rate_limited"
    assert waiting["session_id"] == dispatch.session_id
    assert waiting["details"]["resume_after_s"] > 0
    resumed = _store_event(records, "client.resumed", admission_id=op_id)
    assert (resumed["details"]["resume"], resumed["details"]["resumes"]) == (1, 1)
    started = _operations(
        records, action="store_event", native_event_type="client.started", admission_id=op_id
    )
    assert [r["run_id"] == old_run for r in started] == [True, False]
    assert started[1]["details"]["resumes"] == 1


def test_a_resumed_run_records_no_binding(tmp_path, monkeypatch):
    """The hook finds a resumed admission on each model call, and it binds nothing to it."""
    monkeypatch.setattr(
        "research_core.plugin.executor.profile_env",
        lambda **_: {"IDE_WORKSPACE": str(tmp_path), "IDE_SESSION_ID": "test-harness"},
    )
    dispatch = Dispatch(tmp_path)
    try:
        op_id = dispatch.say("tune the policy")
        dispatch.start_and_fail(op_id, "HTTP 429: slow down", "rate_limit", status_code=429)
        held = dispatch.admission(op_id)
        dispatch.clock.now = datetime.fromisoformat(held["resume_after"]) + timedelta(seconds=1)
        dispatch.tick()
        assert dispatch.admission(op_id)["status"] == "running"
        record_native_message(
            bind_origin=lambda *_: None,
            session_id=dispatch.session_id,
            conversation_history=[{"role": "user", "_row_id": 9}],
        )
        processed(dispatch.server.telemetry, 10)
        records = stored_records(Path(os.environ[STREAM_VARIABLE]))
    finally:
        dispatch.close()
    assert not _operations(records, action="admission", stage="bound")


def test_a_durable_command_names_its_owner_epoch_and_attempts(seam, monkeypatch):
    ctx = seam.ctx
    sessions.register(
        ctx.wstore,
        Actor.harness(),
        session_id="planner",
        profile="orchestrator",
        kind="wake",
        coworld_id=COWORLD,
    )
    tool_ctx = ToolContext(
        role="orchestrator",
        actor=Actor.session("planner", 3),
        workspace=ctx.ws,
        env={"IDE_SESSION_ID": "planner"},
    )
    tool_ctx.store, tool_ctx.wstore = ctx.store, ctx.wstore
    monkeypatch.setattr(transitions, "REQUEST_WAIT_SECONDS", 0)
    args = {"action": "create", "type": "maintenance", "title": "Repair", "description": "It"}
    pending = transitions.request(tool_ctx, "task", args, "repair-1")
    transitions.apply(ctx, "coordinator:one")
    records = seam.records()
    command_id = pending["command_id"]
    requested = _store_event(records, "transition.requested", command_id=command_id)
    assert (requested["tool_name"], requested["session_id"]) == ("task", "planner")
    assert requested["lease_epoch"] == 3
    applied = _store_event(records, "transition.applied", command_id=command_id)
    assert applied["owner_instance"] == "coordinator:one"
    assert applied["lease_epoch"] == 3
    assert applied["details"]["attempts"] == 1


def test_a_failed_command_sends_no_error_text(seam):
    ctx = seam.ctx
    with ctx.store.tx(Actor.harness()) as tx:
        tx.event("transition.retry_waiting", "cmd-1", {"error": SECRET, "retry_at": tx.at})
        tx.event("transition.refused", "cmd-2", {"problems": [SECRET]})
    records = seam.records()
    assert SECRET not in json.dumps(records)
    assert _store_event(records, "transition.retry_waiting")["details"]["retry_at_s"] > 0
    assert _store_event(records, "transition.refused")["command_id"] == "cmd-2"


def test_a_coordinator_report_is_numbers_and_says_nothing_when_unchanged(seam):
    owner = seam.server.coordinator
    ctx = seam.ctx
    reports = []
    for _ in range(3):
        reports.append(owner.advance(ctx))
        reports[-1]["heartbeat"]  # A real report, whose text must not leak below.
    records = seam.records()
    written = _operations(records, action="coordinator", stage="report")
    assert 1 <= len(written) < 3
    first = written[0]
    assert first["owner_instance"] == owner.instance_id
    assert first["coworld_id"] == COWORLD
    assert all(isinstance(value, bool | int | float) for value in first["details"].values())


def test_a_wake_names_its_admission_and_the_events_it_covers(seam):
    ctx = seam.ctx
    with ctx.store.tx(Actor.harness()) as tx:
        seq = tx.event("task.asked", "T-1", {"question": "what next?"})
    coordinator(ctx).advance_events(ctx)
    (wake,) = _operations(seam.records(), action="wake", stage="started")
    [row] = ctx.wstore.q("SELECT * FROM client_admissions")
    assert (wake["admission_id"], wake["session_id"]) == (row["op_id"], row["session_id"])
    assert (wake["native_event_type"], wake["coworld_id"]) == ("task.asked", COWORLD)
    assert (wake["details"]["first_seq"], wake["details"]["batch_size"]) == (seq, 1)


def test_a_failed_worker_names_its_successor(seam):
    ctx = seam.ctx
    predecessor = assigned(ctx)
    ctx.hermes.fail(predecessor["run_id"], "crash")
    owner = coordinator(ctx)
    owner.reconcile()
    owner.advance_attempts(ctx)
    (record,) = _operations(seam.records(), action="execution", stage="successor")
    successor = ctx.wstore.one("SELECT * FROM client_admissions WHERE origin='continuation'")
    assert record["admission_id"] == predecessor["op_id"]
    assert record["execution_attempt_id"] == successor["op_id"]
    assert record["session_id"] == successor["session_id"]
    assert record["parent_session_id"] == predecessor["session_id"]
    assert record["reason"] == "new_session"


def test_a_new_runtime_records_what_it_retired(seam):
    ctx = seam.ctx
    assigned(ctx)
    with ctx.wstore.tx(Actor.harness()) as tx:
        tx.execute("UPDATE client_admissions SET runtime_id='old', execution_uncertain=1")
    Coordinator(ctx.ws, ctx.hermes, "new").reconcile_inputs(ctx.wstore)
    (record,) = _operations(seam.records(), action="coordinator", stage="runtime_recovered")
    assert record["details"] == {"retired_runtimes": 1}


def test_a_failed_pass_records_its_type_and_no_text(seam, monkeypatch):
    def fails() -> None:
        seam.server.stopping.set()  # So that the loop ends after this pass.
        raise RuntimeError(SECRET)

    monkeypatch.setattr(seam.server, "coordinate_once", fails)
    asyncio.run(seam.server.coordinate_forever())
    records = seam.records()
    (record,) = _operations(records, action="coordinator", stage="failed")
    assert (record["native_event_type"], record["reason"]) == ("workflow", "runtimeerror")
    assert SECRET not in json.dumps(records)


def test_report_details_keep_counts_and_drop_text():
    details = observed.report_details(
        {
            "waits": {"errors": [{"error": SECRET}], "resolved": []},
            "merge": {"skipped": f"paused {SECRET}"},
            "store_checks": {"cow:Fake": {"unmarked_writes": 2}},
            "dispatch": {"slots": 2, "deferred": None},
            "heartbeat": {"has_work": True},
        }
    )
    assert details == {
        "waits.errors": 1,
        "waits.resolved": 0,
        "merge.skipped": True,
        "store_checks.cow_fake.unmarked_writes": 2,
        "dispatch.slots": 2,
        "heartbeat.has_work": True,
    }


def test_attachment_outcomes_are_recorded_and_only_the_message_goes_on(seam, monkeypatch):
    outcomes = [{"type": "pdf", "change": "rendered", "size_bytes": 900, "details": {"pages": 2}}]
    native = {"role": "user", "content": [{"type": "text", "text": "saved at: /x"}]}

    def run(*_: Any, **__: Any) -> subprocess.CompletedProcess:
        stdout = json.dumps({"message": native, "outcomes": outcomes})
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(attachments.subprocess, "run", run)
    message = UserMessage.model_validate(
        {
            "role": "user",
            "content": [{"type": "file", "file": {"filename": "a.pdf", "file_data": "data:x"}}],
        }
    )
    prepared = attachments.prepare(
        message,
        ws=seam.ctx.ws,
        env={},
        hermes_bin="hermes",
        admission_id="op",
        session_id="ses",
    )
    assert prepared.root == native
    (record,) = _operations(seam.records(), action="attachment", stage="prepared")
    assert (record["native_event_type"], record["outcome"]) == ("pdf", "rendered")
    assert (record["size_bytes"], record["details"]) == (900, {"pages": 2})
    assert (record["admission_id"], record["session_id"]) == ("op", "ses")


TOOL_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def test_a_workspace_tool_subprocess_records_its_work_and_stdout_stays_its_result(tmp_path, capfd):
    """``run_in_workspace`` takes the last stdout line of a real ``research tool`` process. The
    child writes one completion and its commits with the call IDs. It writes no span of its own:
    since #223 its caller's span covers the call, so its completion needs ``run_tool``'s clock."""
    stream = tmp_path / "telemetry-stream"
    env = {
        **os.environ,
        "IDE_WORKSPACE": str(tmp_path / "ws"),
        "IDE_WORKSPACE_ENV": sys.prefix,
        "IDE_PLATFORM": "fake",
        "IDE_WORKSPACE_ID": "wrk-tool",
        "IDE_ROLE": "orchestrator",
        "IDE_SESSION_ID": "ses_1",
        "IDE_TOOL_CALL_ID": "call_7",
        "TRACEPARENT": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        STREAM_VARIABLE: str(stream),
    }
    ws = layout.from_env(env)
    with Store.open(ws.store_path, kind="workspace") as store:
        sessions.register(
            store,
            Actor.harness(),
            session_id="ses_1",
            profile="orchestrator",
            kind="chat",
        )
    args = {"action": "update", "key": "max_researchers", "value": 2, "cites": "a test"}
    assert json.loads(executor.run_in_workspace("settings", args, env))["ok"] is True
    assert '"name": "execute_tool"' not in capfd.readouterr().err
    records = stored_records(stream)
    (record,) = _operations(records, action="workspace_tool", stage="completed")
    assert record["producer"] == observed.COMMAND_WRITER
    assert record["workspace_id"] == "wrk-tool"
    assert (record["tool_call_id"], record["trace_id"]) == ("call_7", TOOL_TRACE)
    assert (record["tool_name"], record["session_id"]) == ("settings", "ses_1")
    committed = _operations(records, action="store_event")
    assert committed
    assert {(r["tool_call_id"], r["trace_id"]) for r in committed} == {("call_7", TOOL_TRACE)}

    # The whole of its stdout, not only what ``run_in_workspace`` keeps of it.
    stdout = subprocess.run(
        [str(ws.venv_python), "-m", "research_core.cli", "tool", "settings", "--args"]
        + [json.dumps({"action": "read"})],
        env=executor.workspace_process_env(env, ws),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert len(stdout.splitlines()) == 1
    assert json.loads(stdout)["ok"] is True and "service" not in stdout


def test_a_tool_of_an_actions_own_records_the_action_it_stands_for(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(observed, "record", lambda *names, **fields: calls.append((names, fields)))
    name, spec = next((name, spec) for name, spec in _tools().items() if spec.split_from)
    role = min(spec.roles)
    run_tool(name, {}, ToolContext.from_env(role, {"IDE_WORKSPACE": str(tmp_path / "ws")}))
    (fields,) = [fields for names, fields in calls if names == ("workspace_tool", "completed")]
    assert (fields["tool_name"], fields["native_event_type"]) == spec.split_from


def _tools() -> dict[str, Any]:
    from research_core.tools.catalog import _load_tool_modules

    _load_tool_modules()
    return TOOLS


def test_the_handler_passes_the_one_open_native_call_to_the_subprocess(spans):
    """Hermes hands a handler no call ID: the turn's one open call of that tool is the one."""
    hooks, traces = make_instrumentation("researcher")
    hooks["pre_llm_call"](session_id="ses_9", task_id="t", turn_id="turn-9")
    hooks["pre_api_request"](turn_id="turn-9", api_request_id="a", retry_count=0)
    hooks["pre_tool_call"](turn_id="turn-9", tool_call_id="call_9", tool_name="settings")
    assert traces.tool_call("ses_9", "settings")[1] == "call_9"
    hooks["pre_tool_call"](turn_id="turn-9", tool_call_id="call_10", tool_name="settings")
    assert traces.tool_call("ses_9", "settings") is None  # Two open calls say nothing.
    env = executor.tool_env("researcher", "ses_9", {}, tool_call_id="call_9")
    assert env["IDE_TOOL_CALL_ID"] == "call_9"
    assert "IDE_TOOL_CALL_ID" not in executor.tool_env("researcher", "ses_9", {})
