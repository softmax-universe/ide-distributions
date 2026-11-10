"""Capture through Hermes's own hooks, on a real gateway with no telemetry patch and a stub model.

The real plugins run in one gateway, as in the image. The stub drops streams on purpose so that
failed attempts exist to be observed. These cover what a whole Session in
``test_rebuild_real.py`` does not: a dropped stream, a rebuilt prompt, overlapping runs and a
gateway killed during a call. ``plugin/capture.py`` says which native hook records what.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time

import pytest

from research_core.watcher.hermes_api import mint_session_id
from tests.hermes.capture_rig import chunk, start_gateway, stream_reply, stub_server, tool_call
from tests.hermes.test_spike_s1 import free_port, hermes_python
from tests.telemetry.conftest import objects_of, stored_records

REASONING = "thinking before the drop"
VISIBLE_TEXT = "text the user saw before the drop"
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
SOUL_TEXT = "REBUILT SOUL TEXT FOR THE TEST"


class DroppingStub:
    """OpenRouter's chat endpoint. The latest user message picks the script of a turn, and a
    script that drops does so on its first request only."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.dropped: set[str] = set()
        self.hang = threading.Event()
        self.title_gate = threading.Event()
        self.server = stub_server(self.answer)
        self.url = f"http://openrouter.ai:{self.server.server_address[1]}/api/v1"

    def answer(self, handler, raw: bytes, _arrived: float) -> None:
        body = json.loads(raw)
        messages = body.get("messages")
        if messages is None:
            handler.send_error(404)
            return
        if not body.get("tools"):
            if b"LATE_TITLE" in raw:
                self.title_gate.wait(60)
            stream_reply(handler, [chunk({"content": "a title"}), chunk({}, "stop", USAGE)])
            return
        script = str([m for m in messages if m["role"] == "user"][-1]["content"])
        with self.lock:
            first_time = script not in self.dropped
            self.dropped.add(script)
        if "HANG_IN_THE_CALL" in script:
            self.hang.wait(60)
            return
        if messages[-1]["role"] == "tool" or "plain answer" in script:
            stream_reply(handler, [chunk({"content": "done"}), chunk({}, "stop", USAGE)])
        elif first_time and "drop mid tool call" in script:
            stream_reply(
                handler,
                [
                    chunk({"reasoning": REASONING}),
                    chunk({"tool_calls": [tool_call(0, "call_cut", "settings", '{"act')]}),
                ],
                pause_s=0.05,
                drop=True,
            )
        elif first_time and "drop after text" in script:
            stream_reply(handler, [chunk({"content": VISIBLE_TEXT})], pause_s=0.05, drop=True)
        elif "use the tools" in script:
            call = tool_call(0, "call_read", "settings", '{"action": "read"}')
            stream_reply(handler, [chunk({"tool_calls": [call]}), chunk({}, "tool_calls", USAGE)])
        else:
            stream_reply(handler, [chunk({"content": "done"}), chunk({}, "stop", USAGE)])


@pytest.fixture(scope="module")
def stub():
    model = DroppingStub()
    yield model
    model.hang.set()
    model.title_gate.set()
    model.server.shutdown()


@pytest.fixture(scope="module")
def gateway(hermes_bin, tmp_path_factory, stub):
    running = start_gateway(
        tmp_path_factory.mktemp("native"), hermes_python(hermes_bin), stub.url, free_port()
    )
    yield running
    running.stop()


def open_session(gateway) -> str:
    session_id = mint_session_id()
    gateway.request("POST", "orchestrator", "/api/sessions", {"id": session_id})
    return session_id


def start_run(gateway, session_id: str, text: str) -> str:
    body = {"input": text, "session_id": session_id}
    return gateway.request("POST", "orchestrator", "/v1/runs", body)["run_id"]


def wait_run(gateway, run_id: str) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        status = gateway.request("GET", "orchestrator", f"/v1/runs/{run_id}")["status"]
        if status in ("completed", "failed", "cancelled", "interrupted"):
            assert status == "completed", gateway.log_tail()
            return
        time.sleep(0.1)
    raise AssertionError(gateway.log_tail())


def records_of(gateway, session_id: str, *, wait_for: tuple[str, str] | None = None) -> list:
    """The Session's records once ``wait_for`` (an action and stage) is among them."""
    deadline = time.monotonic() + 20
    while True:
        records = [
            r for r in stored_records(gateway.stream) if r.get("hermes_session_id") == session_id
        ]
        found = wait_for is None or operations(records, *wait_for)
        if found or time.monotonic() > deadline:
            return records
        time.sleep(0.1)


def operations(records: list, action: str, stage: str | None = None) -> list:
    return [
        r
        for r in records
        if r["family"] == "operation" and r["action"] == action and stage in (None, r["stage"])
    ]


def text_of(gateway, record: dict, name: str) -> bytes:
    digest = next(ref["digest"] for ref in record["content_refs"] if ref["name"] == name)
    return objects_of(stored_records(gateway.stream))[digest]


def test_every_record_names_its_own_turns_trace_while_turns_overlap(gateway):
    """A turn that serves no client's message is a trace of its own."""
    session_id, other_session_id = open_session(gateway), open_session(gateway)
    run_id = start_run(gateway, session_id, "use the tools")
    other_run_id = start_run(gateway, other_session_id, "plain answer please")
    wait_run(gateway, run_id)
    wait_run(gateway, other_run_id)
    records = records_of(gateway, session_id, wait_for=("message", "committed"))
    others = records_of(gateway, other_session_id, wait_for=("message", "committed"))
    for action, stage in [
        ("model_request", "started"),
        ("model_attempt", "request"),
        ("model_attempt", "response"),
        ("tool", "completed"),
        ("tool", "result"),
        ("message", "committed"),
    ]:
        found = operations(records, action, stage)
        assert found and all("trace_id" in r for r in found), (action, stage)
    (trace_id,) = {r["trace_id"] for r in operations(records, "turn", "ended")}
    (other_trace_id,) = {r["trace_id"] for r in operations(others, "turn", "ended")}
    assert trace_id != other_trace_id
    # A title can begin before the turn's span: then it names none, never another trace.
    assert {r["trace_id"] for r in records if "trace_id" in r} == {trace_id}
    # A tool's records name its own span; its proposal names the reply that asked for it.
    (tool_span,) = {r["span_id"] for r in operations(records, "tool", "completed")}
    assert {r["span_id"] for r in operations(records, "tool", "result")} == {tool_span}
    # The result is the one the model got, joined to its committed row by native ID.
    result = operations(records, "tool", "result")[0]
    rows = {r["hermes_message_id"] for r in operations(records, "message", "committed")}
    assert result["tool_call_id"] == "call_read" and result["hermes_message_id"] in rows
    # The tool's handler ran in the native call's span, the turn's one open call of that tool.
    workspace = [r for r in stored_records(gateway.stream) if r.get("action") == "workspace_tool"]
    assert {(r.get("trace_id"), r.get("tool_call_id")) for r in workspace} >= {
        (trace_id, "call_read")
    }


def test_a_title_that_ends_after_its_turn_names_that_turns_trace_or_none(gateway, stub):
    """Hermes starts the title call at the turn's start, which can be before the turn's span starts
    at its first model request. Then its records name no span rather than a guessed one; never
    another turn's, even one running when the title ends."""
    session_id, other_session_id = open_session(gateway), open_session(gateway)
    run_id = start_run(gateway, session_id, "plain answer LATE_TITLE")
    wait_run(gateway, run_id)
    other_run_id = start_run(gateway, other_session_id, "plain answer two")
    wait_run(gateway, other_run_id)
    stub.title_gate.set()
    deadline = time.monotonic() + 20
    while True:
        titles = [
            r
            for r in operations(records_of(gateway, session_id), "model_attempt")
            if str(r.get("model_attempt_id", "")).startswith("aux-")
        ]
        if "response" in {r["stage"] for r in titles} or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert {r["stage"] for r in titles} >= {"request", "response"}
    (turn_trace,) = {r["trace_id"] for r in operations(records_of(gateway, session_id), "turn")}
    assert {r.get("trace_id") for r in titles} <= {turn_trace, None}
    assert len({r.get("trace_id") for r in titles}) == 1  # Its end keeps what its start had.


def test_a_dropped_stream_keeps_its_reasoning_and_the_retry_completes(gateway):
    session_id = open_session(gateway)
    wait_run(gateway, start_run(gateway, session_id, "drop mid tool call now"))
    records = records_of(gateway, session_id, wait_for=("turn", "ended"))
    partial = [r for r in operations(records, "model_attempt", "response") if r.get("partial")]
    assert partial, [(r["action"], r["stage"]) for r in records]
    assert json.loads(text_of(gateway, partial[0], "message"))["reasoning_content"] == REASONING
    completed = [
        r for r in operations(records, "model_attempt", "response") if not r.get("partial")
    ]
    assert completed and completed[-1]["outcome"] == "completed"


def test_a_stream_dropped_after_visible_text_keeps_that_text(gateway):
    """Hermes continues from text a stream showed before it broke: it becomes history, which the
    next request sends, so that request's record holds it."""
    session_id = open_session(gateway)
    wait_run(gateway, start_run(gateway, session_id, "drop after text now"))
    records = records_of(gateway, session_id, wait_for=("turn", "ended"))
    requests = operations(records, "model_attempt", "request")
    assert len(requests) >= 2
    objects = objects_of(stored_records(gateway.stream))
    assert any(VISIBLE_TEXT.encode() in body for body in objects.values())
    # The continuation's request binds the user's row, not Hermes's uncommitted nudge.
    assert "record_native_message" not in gateway.log.read_text()


def test_a_rebuilt_prompt_attributes_the_new_soul(gateway, hermes_bin):
    session_id = open_session(gateway)
    wait_run(gateway, start_run(gateway, session_id, "plain answer one"))
    profile = gateway.home / "profiles" / "orchestrator"
    original = (profile / "SOUL.md").read_text()
    (profile / "SOUL.md").write_text(SOUL_TEXT + "\n")
    try:
        refreshed = subprocess.run(
            [str(hermes_bin), "sessions", "refresh-prompts"],
            env={**gateway_env(gateway), "HERMES_HOME": str(profile)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
        wait_run(gateway, start_run(gateway, session_id, "plain answer two"))
    finally:
        (profile / "SOUL.md").write_text(original)
    records = records_of(gateway, session_id, wait_for=("turn", "ended"))
    last = operations(records, "model_attempt", "request")[-1]
    manifest = json.loads(text_of(gateway, last, "request_manifest"))
    objects = objects_of(stored_records(gateway.stream))
    sections = json.loads(objects[manifest["prompt_sections_digest"]])["sections"]
    assert {section["name"] for section in sections} >= {"soul", "plugin_sections"}


def gateway_env(gateway) -> dict:
    return {**os.environ, "HERMES_HOME": str(gateway.home), "HERMES_REAL_HOME": str(gateway.home)}


def test_a_request_is_on_the_stream_before_its_call_ends(hermes_bin, tmp_path, stub):
    """No exit hook is needed for what matters most: the request is written before the call, so
    a gateway killed during it leaves the request behind."""
    gateway = start_gateway(tmp_path, hermes_python(hermes_bin), stub.url, free_port())
    session_id = open_session(gateway)
    start_run(gateway, session_id, "HANG_IN_THE_CALL")
    records = records_of(gateway, session_id, wait_for=("model_attempt", "request"))
    gateway.process.send_signal(signal.SIGKILL)
    gateway.process.wait(30)
    requests = operations(records, "model_attempt", "request")
    assert requests
    objects = objects_of(stored_records(gateway.stream))
    assert any(b"HANG_IN_THE_CALL" in body for body in objects.values())
