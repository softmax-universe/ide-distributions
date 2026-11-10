"""A model's reasoning reaches a request's AIDE event log while the model streams it.

The pinned gateway runs the shipped plugin against a stub model that streams reasoning before a
tool call and before its answer. The AIDE backend's relay server and its follower of the run are
the image's own (``image/aide.py``); only the seam is a recorder of what the follower appends.
The run is admitted to Hermes's session authority, the branch whose run stream has no reasoning.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime
from itertools import groupby

import httpx

from tests.hermes.capture_rig import (
    API_KEY,
    chunk,
    start_gateway,
    stream_reply,
    stub_server,
    tool_call,
    whole_reply,
)
from tests.hermes.test_spike_s1 import free_port, hermes_python
from tests.test_aide_backend import Availability, aide

SESSION = "api_reasoning_relay"
CHUNKS = 40  # reasoning deltas in each model reply
PAUSE_S = 0.02  # between deltas, as a model streams them
USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


class ThinkingModel:
    """Reasons, then calls ``settings``; after the result, reasons again and answers.

    The first reply waits for ``bound``: until the backend follows the run, its reasoning has no
    log to go to."""

    def __init__(self) -> None:
        self.bound = threading.Event()
        self.server = stub_server(self.answer)
        self.url = f"http://openrouter.ai:{self.server.server_address[1]}/api/v1"

    def answer(self, handler, raw: bytes, _arrived: float) -> None:
        body = json.loads(raw)
        if not body.get("tools"):
            whole_reply(handler, "a title", USAGE)
            return
        thinking = [chunk({"reasoning": f"thought {n}. "}) for n in range(CHUNKS)]
        if body["messages"][-1]["role"] == "tool":
            stream_reply(
                handler,
                thinking + [chunk({"content": "all done"}), chunk({}, "stop", USAGE)],
                pause_s=PAUSE_S,
            )
            return
        self.bound.wait(60)
        call = tool_call(0, "call_read", "settings", '{"action": "read"}')
        stream_reply(
            handler,
            thinking + [chunk({"tool_calls": [call]}), chunk({}, "tool_calls", USAGE)],
            pause_s=PAUSE_S,
        )


def test_reasoning_streams_into_the_request_log_before_the_tool(hermes_bin, tmp_path):
    stub = ThinkingModel()
    relay_port = free_port()
    gateway = start_gateway(
        tmp_path,
        hermes_python(hermes_bin),
        stub.url,
        free_port(),
        extra_env={"IDE_REASONING_RELAY": f"127.0.0.1:{relay_port}"},
    )
    appended: list[dict] = []

    def seam(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/message-events":
            appended.append(json.loads(request.content))
            return httpx.Response(200, json={})
        assert request.url.path == "/session-pending", request.url
        return httpx.Response(200, json={"pending": True})  # no transcript to announce here

    async def main() -> tuple[dict, aide.Backend]:
        async with (
            httpx.AsyncClient(base_url="http://seam", transport=httpx.MockTransport(seam)) as s,
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{gateway.port}/p/orchestrator/",
                headers={"Authorization": f"Bearer {API_KEY}"},
            ) as native,
            asyncio.TaskGroup() as work,
        ):
            backend = aide.Backend(s, native, Availability(), work)
            server = await asyncio.start_server(backend.relay, "127.0.0.1", relay_port)
            started = await native.post("v1/runs", json={"input": "hi", "session_id": SESSION})
            run_id = started.raise_for_status().json()["run_id"]
            now = datetime.now(UTC).isoformat()
            request = aide.Admission(
                op_id="op-1",
                session_id=SESSION,
                status="running",
                input=None,
                error=None,
                error_class=None,
                resume_after=None,
                created_at=now,
                updated_at=now,
                revision=1,
                resumes=0,
                runs=[],
                logged=None,
            )
            run = aide.Run(attempt=0, run_id=run_id, steered=False)
            follower = asyncio.create_task(backend.follow(request, run, aide.Progress(0), 0))
            while not backend.thinking:
                await asyncio.sleep(0.01)
            stub.bound.set()
            assert (await follower) == (True, 0)
            status = (await native.get(f"v1/runs/{run_id}")).json()
            server.close()
            return status, backend

    try:
        status, backend = asyncio.run(main())
    finally:
        gateway.stop()
        stub.server.shutdown()
    # Admitted to the session authority: the branch whose run stream carries no reasoning.
    assert status["status"] == "completed" and status["admission_id"], status
    logged = [body["event"] for body in appended]
    kinds = [event["kind"] for event in logged]
    print(f"{len(logged)} events, {backend.relayed} relayed reasoning deltas: {kinds}")
    assert [event["seq"] for event in logged] == list(range(1, len(logged) + 1))
    first_tool = kinds.index("tool.started")
    assert "reasoning" in kinds[:first_tool]
    reasoning = [event["text"] for event in logged if event["kind"] == "reasoning"]
    # Every delta of both replies, in order, merged into a few events.
    expected = "".join(f"thought {n}. " for n in range(CHUNKS))
    assert "".join(reasoning) == expected * 2
    assert backend.relayed == 2 * CHUNKS
    assert len(reasoning) < 2 * CHUNKS / 4
    # A reply that reasons for longer than REASONING_MERGE_SECONDS logs more than one event.
    runs = [kind for kind, _ in groupby(kinds)]
    assert runs == ["reasoning", "tool.started", "tool.completed", "reasoning", "text.delta"]
