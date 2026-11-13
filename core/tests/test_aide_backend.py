"""The image's AIDE 1.0.0 backend over a real seam and a scripted gateway."""

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from research_core import run_failures
from research_core.serve import SeamServer
from research_core.store import admissions, goals, sessions, settings, tasks
from research_core.store.db import DEFAULT_MAX_HOLD_S, quiesce_busy_timeout_ms
from research_core.watcher.hermes_api import FakeHermesRuns
from tests.test_serve import COWORLD_ID, HARNESS, make_ctx

IMAGE = Path(__file__).resolve().parents[2] / "image"


class Availability:
    """The image's inference gate; its real module needs the Hermes interpreter's dotenv."""

    def __init__(self):
        self.open = True

    def available(self):
        return self.open

    def blocking(self):
        return None


sys.modules.setdefault("inference", types.SimpleNamespace(Availability=Availability))
sys.path.insert(0, str(IMAGE))
import aide  # noqa: E402

from research_core.plugin.reasoning_relay import Relay  # noqa: E402

WORKSPACE = {"scope": "workspace", "coworld": None, "league": None}
LEAGUE = {"scope": "coworld", "coworld": COWORLD_ID, "league": "league_1"}
FRONTEND, OTHER = uuid4(), uuid4()
SCENARIO_LIMIT_S = 30


class Socket:
    def __init__(self):
        self.frames: list[dict] = []

    async def send(self, text: str) -> None:
        frame = json.loads(text)
        assert len(json.dumps(frame["message"], separators=(",", ":")).encode()) <= aide.MAX_BYTES
        self.frames.append(frame)

    def notes(self, method: str, frontend=FRONTEND) -> list[dict]:
        return [
            f["message"]["params"]
            for f in self.frames
            if f["frontend_id"] == str(frontend) and f["message"].get("method") == method
        ]


class Gateway:
    """The gateway routes the backend reads: transcripts, run state and run event streams.

    Each run's events come from ``script`` once. Like Hermes's per-run buffer, the gateway
    retains only the newest ``capacity`` events a run has reached, in memory: a read from
    before them starts with ``replay.truncated``, and ``restart`` loses every run. Their ``say``
    messages join the transcript when the run first reaches them. A ``gate`` entry holds the
    run open until it is set.
    """

    def __init__(self, hermes: FakeHermesRuns):
        self.hermes = hermes
        self.transcripts: dict[str, list[dict]] = {}
        self.titles: dict[str, str] = {}
        self.controls: dict[str, list[dict]] = {}
        self.event_requests: list[tuple[str, str]] = []
        self.drop_after: int | None = None
        self.answers: list[tuple[str, dict]] = []
        self.runs: dict[str, list[dict]] = {}
        self.retired: set[str] = set()
        self.gate = asyncio.Event()
        self.capacity = 1000
        self.reached: dict[str, int] = {}  # run id -> the newest seq the run has produced
        # How a ``think`` entry reaches the backend: the gateway plugin's relay, set by a test.
        self.think = None

    def restart(self) -> None:
        """The gateway process restarts: its in-memory runs and their buffers are gone."""
        self.retired |= set(self.runs)

    def say(self, session_id: str, **message) -> None:
        rows = self.transcripts.setdefault(session_id, [])
        number = 100 + sum(len(t) for t in self.transcripts.values())
        rows.append({"id": number, "timestamp": 1793980800.0 + number, **message})

    def script(self, run_id: str) -> list[dict]:
        call = {"id": "call_1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}
        done = {"role": "assistant", "content": "Done", "reasoning": "Listing first."}
        return [
            {"event": "run.started"},
            {"event": "reasoning.available", "text": "Listing first."},
            {"event": "message.delta", "delta": "Hel"},
            {"event": "message.delta", "delta": "lo"},
            {
                "event": "tool.started",
                "tool": "ls",
                "preview": "ls",
                "tool_call_id": "call_1",
                "say": [{"role": "assistant", "content": None, "tool_calls": [call]}],
            },
            {
                "event": "tool.completed",
                "tool": "ls",
                "tool_call_id": "call_1",
                "say": [{"role": "tool", "content": "files", "tool_call_id": "call_1"}],
            },
            {"event": "message.delta", "delta": "Done"},
            {"event": "run.completed", "say": [done]},
        ]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/p/orchestrator/")
        if path.startswith("api/sessions/"):
            session_id = path.split("/")[2]
            if session_id not in self.hermes.sessions:
                return httpx.Response(404, json={"error": "Session not found"})
            if path == f"api/sessions/{session_id}":
                return httpx.Response(
                    200, json={"session": {"id": session_id, "title": self.titles.get(session_id)}}
                )
            # Hermes ``order=latest``: offset and limit count back from the newest, and the page
            # comes back oldest first.
            assert request.url.params["order"] == "latest"
            rows = list(reversed(self.transcripts.get(session_id, [])))
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            return httpx.Response(200, json={"data": rows[offset : offset + limit][::-1]})
        run_id = path.split("/")[2]
        if run_id in self.retired:
            return httpx.Response(404, json={"error": "Run not found"})
        if path.endswith("/events"):
            last_seq = int(request.url.params["last_seq"])
            self.event_requests.append((run_id, request.url.params["last_seq"]))
            return httpx.Response(200, content=self.stream(run_id, last_seq))
        if path.endswith(("/approval", "/clarify")):
            self.answers.append((path.rsplit("/", 1)[1], json.loads(request.content)))
            self.controls[run_id] = []
            return httpx.Response(200, json={"run_id": run_id, "status": "resolved"})
        return httpx.Response(
            200,
            json={
                "pending_controls": self.controls.get(run_id, []),
                "session_id": self.hermes.runs[run_id].session_id,
            },
        )

    async def stream(self, run_id: str, last_seq: int):
        if run_id not in self.runs:
            self.runs[run_id] = self.script(run_id)
        yield b": open\n\n"
        oldest = self.reached.get(run_id, -1) - self.capacity + 1
        if last_seq + 1 < oldest:
            yield f"data: {json.dumps({'event': 'replay.truncated'})}\n\n".encode()
        for seq, event in enumerate(self.runs[run_id]):
            if seq <= max(last_seq, oldest - 1):
                continue
            self.reached[run_id] = max(self.reached.get(run_id, -1), seq)
            if event.get("gate"):
                await self.gate.wait()
                continue
            if "think" in event:
                await self.think(self.hermes.runs[run_id].session_id, event)
                continue
            if self.drop_after is not None and seq > self.drop_after:
                self.drop_after = None
                raise httpx.ReadError("gateway stream dropped")
            for message in event.pop("say", []):
                self.say(self.hermes.runs[run_id].session_id, **message)
            if event["event"] == "run.completed" and self.hermes.runs[run_id].status == "running":
                self.hermes.complete(run_id, "Done")
            payload = {**event, "run_id": run_id, "seq": seq, "timestamp": 1793980800.0 + seq}
            yield f"id: {seq}\ndata: {json.dumps(payload)}\n\n".encode()
            await asyncio.sleep(0)
        yield b": stream closed\n\n"


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC)

    def __call__(self):
        return self.now


@pytest.fixture
def system(tmp_path):
    ctx = make_ctx(tmp_path, coworld=True)
    hermes = FakeHermesRuns()
    clock = Clock()
    server = SeamServer(ws=ctx.workspace, hermes=hermes, env=dict(ctx.env), clock=clock)
    server.test_clock = clock
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield ctx, hermes, server
    server.shutdown()
    worker.join(timeout=10)


def run(system, scenario, inference=None, seam_transport=None, gateway=None):
    ctx, hermes, server = system
    gateway = gateway or Gateway(hermes)

    async def main():
        async with (
            httpx.AsyncClient(base_url=server.url, transport=seam_transport) as seam,
            httpx.AsyncClient(
                base_url="http://gateway/p/orchestrator/",
                transport=httpx.MockTransport(gateway.handle),
            ) as native,
            asyncio.TaskGroup() as work,
        ):
            backends, watchers = [], []

            def tasks():
                return (
                    watchers
                    + [task for old in backends for task in old.consumers.values()]
                    + [
                        feed.watcher
                        for old in backends
                        for feed in old.boards.values()
                        if feed.watcher is not None
                    ]
                )

            async def stop():
                # A cancel can be lost while a task reads the seam stream. Keep cancelling
                # until every old worker exits before another backend can write its logs.
                while not all(task.done() for task in tasks()):
                    for task in tasks():
                        task.cancel()
                    await asyncio.sleep(0.05)

            async def restart():
                """A new backend process: every worker of the previous one has exited."""
                await stop()
                backend = aide.Backend(seam, native, inference or Availability(), work)
                backend.socket = Socket()
                backend.restart = restart
                backends.append(backend)
                watchers.append(work.create_task(backend.watch()))
                return backend

            try:
                async with asyncio.timeout(SCENARIO_LIMIT_S):
                    await scenario(await restart(), gateway, ctx, hermes)
            finally:
                await stop()

    asyncio.run(main())


_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("uuid")
def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        UUID(value)
    except ValueError:
        return False
    return value == value.lower()


@_FORMAT_CHECKER.checks("date-time")
def _is_rfc3339_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if "T" not in value and "t" not in value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


class _AideContract:
    def __init__(self, spec_path: Path):
        document = json.loads(spec_path.read_bytes())
        self._uri = spec_path.as_uri()
        self._registry = Registry().with_resource(self._uri, DRAFT202012.create_resource(document))
        self._validators: dict[str, Draft202012Validator] = {}
        self.message_schemas: dict[str, str] = {}
        self.param_schemas: dict[str, str] = {}
        self.reply_envelopes: dict[str, dict] = {}
        self.result_schemas: dict[str, str] = {}
        for operation in document["operations"].values():
            if operation["channel"]["$ref"] != "#/channels/frontend":
                continue
            message_obj = self._resolve(document, operation["messages"][0])
            payload = self._resolve(document, message_obj["payload"])
            if "method" not in payload["properties"] or operation["tags"][0]["name"].startswith(
                "Relay"
            ):
                continue
            name = payload["properties"]["method"]["const"]
            self.message_schemas[name] = message_obj["payload"]["$ref"]
            self.param_schemas[name] = payload["properties"]["params"]["$ref"]
            if "reply" in operation:
                reply_msg = self._resolve(document, operation["reply"]["messages"][0])
                reply_payload = reply_msg["payload"]
                self.reply_envelopes[name] = reply_payload
                self.result_schemas[name] = reply_payload["properties"]["result"]["$ref"]

    @staticmethod
    def _resolve(document: dict, value: dict) -> dict:
        while "$ref" in value:
            reference = value["$ref"]
            value = document
            for segment in reference[2:].split("/"):
                value = value[segment.replace("~1", "/").replace("~0", "~")]
        return value

    def _validate(self, ref: str, instance: object) -> None:
        validator = self._validators.get(ref)
        if validator is None:
            validator = Draft202012Validator(
                {"$ref": f"{self._uri}{ref}"},
                registry=self._registry,
                format_checker=_FORMAT_CHECKER,
            )
            self._validators[ref] = validator
        validator.validate(instance)

    def _validate_inline(self, key: str, schema: dict, instance: object) -> None:
        validator = self._validators.get(key)
        if validator is None:
            resolved = json.loads(json.dumps(schema).replace('"#/', f'"{self._uri}#/'))
            validator = Draft202012Validator(
                resolved,
                registry=self._registry,
                format_checker=_FORMAT_CHECKER,
            )
            self._validators[key] = validator
        validator.validate(instance)

    def validate_request(self, frame: dict) -> None:
        self._validate("#/components/schemas/Request", frame)
        self._validate(self.message_schemas[frame["method"]], frame)

    def validate_result(self, method: str, frame: dict) -> None:
        self._validate_inline(f"reply:{method}", self.reply_envelopes[method], frame)
        self._validate(self.result_schemas[method], frame["result"])

    def validate_error(self, frame: object) -> None:
        self._validate("#/components/schemas/Error", frame)

    def validate_notification(self, frame: dict) -> None:
        self._validate("#/components/schemas/Notification", frame)
        method = frame["method"]
        self._validate(self.message_schemas[method], frame)
        self._validate(self.param_schemas[method], frame["params"])


@lru_cache(maxsize=1)
def _aide_contract() -> _AideContract:
    return _AideContract(IMAGE / "contract/aide-1.0.0/asyncapi.json")


@pytest.fixture(autouse=True)
def _validate_every_aide_frame(monkeypatch):
    contract = _aide_contract()
    methods: dict[str, str] = {}
    real_send = aide.Backend.send
    real_answer = aide.Backend.answer
    real_call = call

    async def checked_call(backend, method, params, *, request_id=None, frontend=FRONTEND):
        rid = request_id or str(uuid4())
        methods[rid] = method
        request = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        reply = await real_call(backend, method, params, request_id=rid, frontend=frontend)
        if "result" in reply:
            contract.validate_request(request)
        return reply

    async def checked_answer(self, socket, frame):
        if isinstance(frame.message, dict):
            rid = frame.message.get("id")
            method = frame.message.get("method")
            if isinstance(rid, str) and isinstance(method, str):
                methods.setdefault(rid, method)
        return await real_answer(self, socket, frame)

    async def checked_send(self, socket, frontend, message):
        if "method" in message and "id" not in message:
            contract.validate_notification(message)
        elif "error" in message:
            contract.validate_error(message)
        elif "result" in message:
            method = methods.get(message["id"])
            assert method is not None, f"Result for unknown request id {message['id']}"
            contract.validate_result(method, message)
        else:
            raise AssertionError(f"Unclassified outgoing message: {message}")
        return await real_send(self, socket, frontend, message)

    monkeypatch.setattr(sys.modules[__name__], "call", checked_call)
    monkeypatch.setattr(aide.Backend, "answer", checked_answer)
    monkeypatch.setattr(aide.Backend, "send", checked_send)


def test_aide_contract_validator_rejects_invalid_frames():
    contract = _aide_contract()
    valid_note = {
        "jsonrpc": "2.0",
        "method": "state.changed",
        "params": {
            "resource": "sessions",
            "epoch": "2026-04-01T00:00:00.000Z",
            "previous_revision": 0,
            "revision": 1,
            "change": "upsert",
            "item": {
                "id": "s1",
                "parent_session_id": None,
                "started_at": "2026-04-01T00:00:00Z",
                "ended_at": None,
                "active_request_ids": [],
                "pending": [],
                "scope": WORKSPACE,
                "requests": [],
            },
        },
    }
    contract.validate_notification(valid_note)
    with pytest.raises(ValidationError):
        contract.validate_notification(
            valid_note | {"params": valid_note["params"] | {"extra_field": True}}
        )

    valid_result = {
        "jsonrpc": "2.0",
        "id": str(uuid4()),
        "result": {
            "accepted": True,
            "session_id": "s1",
            "request_id": str(uuid4()),
        },
    }
    contract.validate_result("chat.send", valid_result)
    with pytest.raises(ValidationError):
        contract.validate_result(
            "chat.send",
            valid_result | {"result": valid_result["result"] | {"accepted": False}},
        )


async def call(backend, method, params, *, request_id=None, frontend=FRONTEND):
    request_id = request_id or str(uuid4())
    message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    await backend.answer(backend.socket, aide.Frame(frontend_id=frontend, message=message))
    replies = [f["message"] for f in backend.socket.frames if f["message"].get("id") == request_id]
    return replies[-1]


async def until(check):
    while not check():
        await asyncio.sleep(0.02)


async def opened(backend, scope=WORKSPACE):
    reply = await call(backend, "session.open", {"scope": scope})
    return reply["result"]["session"]["id"]


def say(text):
    return [{"role": "user", "content": text}]


def code(reply):
    return reply["error"]["data"]["code"]


def test_session_open_replays_its_session_and_refuses_reused_ids(system):
    async def scenario(backend, gateway, ctx, hermes):
        request_id = str(uuid4())
        first = await call(backend, "session.open", {"scope": LEAGUE}, request_id=request_id)
        session = first["result"]["session"]
        assert session["scope"] == LEAGUE and session["pending"] == []
        again = await call(backend, "session.open", {"scope": LEAGUE}, request_id=request_id)
        assert again["result"]["session"]["id"] == session["id"]
        reused = await call(backend, "session.open", {"scope": WORKSPACE}, request_id=request_id)
        assert code(reused) == "id_reused"
        missing = await call(
            backend, "state.get", {"resource": "sessions", "id": "nope", **{"scope": WORKSPACE}}
        )
        assert code(missing) == "not_found"

    run(system, scenario)
    ctx = system[0]
    assert ctx.wstore.one("SELECT COUNT(*) FROM client_admissions WHERE kind='open'")[0] == 1


def events_read(backend, session_id, request_id, after=None, **extra):
    params = {
        "session_id": session_id,
        "request_id": request_id,
        "after": after or {"attempt": 0, "seq": 0},
        "scope": WORKSPACE,
    }
    return call(backend, "chat.events", params | extra)


def cursor(event):
    return (event["attempt"], event["seq"])


def test_chat_round_trip_pushes_what_the_reads_return_to_every_subscriber(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        watching = await call(
            backend,
            "state.get",
            {"resource": "sessions", "id": session_id, "scope": WORKSPACE},
            frontend=OTHER,
        )
        assert watching["result"]["item"]["id"] == session_id
        assert "title" not in watching["result"]["item"]
        request_id = str(uuid4())
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        accepted = await call(backend, "chat.send", params, request_id=request_id)
        assert accepted["result"] == {
            "accepted": True,
            "session_id": session_id,
            "request_id": request_id,
        }
        socket = backend.socket
        await until(lambda: socket.notes("chat.finished") and socket.notes("chat.finished", OTHER))
        (run_id,) = hermes.runs
        pushed = [n["event"] for n in socket.notes("chat.event")]
        assert [e["kind"] for e in pushed] == [
            "reasoning",
            "text.delta",
            "tool.started",
            "tool.completed",
            "text.delta",
            "finished",
        ]
        # Consecutive deltas merge into one event; every event takes the next seq of its attempt.
        assert [e.get("delta") for e in pushed if e["kind"] == "text.delta"] == ["Hello", "Done"]
        assert [cursor(e) for e in pushed] == [(0, n) for n in range(1, 7)]
        assert pushed[2] | {"at": None} == {
            "attempt": 0,
            "seq": 3,
            "at": None,
            "kind": "tool.started",
            "call_id": "call_1",
            "name": "ls",
            "arguments": "ls",
            "truncated": True,
        }
        for frontend in (FRONTEND, OTHER):
            chunks = socket.notes("chat.chunk", frontend)
            assert [(c["completion_id"], c["seq"]) for c in chunks] == [
                (f"{run_id}:2", 1),
                (f"{run_id}:5", 1),
            ]
            # A chunk mirrors the text.delta at its cursor.
            assert [
                (c["cursor"]["seq"], c["chunk"]["choices"][0]["delta"]["content"]) for c in chunks
            ] == [(e["seq"], e["delta"]) for e in pushed if e["kind"] == "text.delta"]
            messages = socket.notes("chat.message", frontend)
            assert [m["message"]["role"] for m in messages] == ["assistant", "tool", "assistant"]
            assert all(m["request_id"] == request_id and m["kind"] == "message" for m in messages)
            assert messages[-1]["reasoning"] == "Listing first." and messages[-1]["created_at"]
            (finished,) = socket.notes("chat.finished", frontend)
            assert finished["status"] == "completed"
            assert finished["cursor"] == {"attempt": 0, "seq": 6}
            changes = socket.notes("state.changed", frontend)
            assert any(request_id in c["item"]["active_request_ids"] for c in changes)
            assert all(c["previous_revision"] < c["revision"] for c in changes)
            assert [c["previous_revision"] for c in changes[1:]] == [
                c["revision"] for c in changes[:-1]
            ]
        read = (await events_read(backend, session_id, request_id))["result"]
        assert read["events"] == pushed and read["done"] is True
        assert read["next"] == {"attempt": 0, "seq": 6}
        assert (await events_read(backend, session_id, request_id))["result"] == read
        tail = await events_read(backend, session_id, request_id, {"attempt": 0, "seq": 5})
        assert tail["result"]["events"] == pushed[5:]
        paged = (await events_read(backend, session_id, request_id, limit=2))["result"]
        assert paged["events"] == pushed[:2] and paged["done"] is False
        assert paged["next"] == {"attempt": 0, "seq": 2}
        past = (await events_read(backend, session_id, request_id, read["next"]))["result"]
        assert past == read | {"events": [], "next": read["next"], "done": True}
        record = (
            await call(
                backend, "state.get", {"resource": "requests", "id": request_id, "scope": WORKSPACE}
            )
        )["result"]
        assert record["item"]["revision"] == finished["revision"] == record["revision"]
        assert record["item"]["status"] == "completed" and record["item"]["in_history"] is True
        assert "messages" not in record["item"]
        history = await call(
            backend, "chat.history", {"session_id": session_id, "scope": WORKSPACE}
        )
        items = history["result"]["items"]
        assert [i["message"]["role"] for i in items] == ["assistant", "tool", "assistant"]
        assert [i["sequence"] for i in items] == sorted(i["sequence"] for i in items)
        assert history["result"]["next_cursor"] is None
        # A retry of the accepted request returns the original acceptance and pushes nothing.
        frames = len(socket.frames)
        again = await call(backend, "chat.send", params, request_id=request_id)
        assert again["result"] == accepted["result"]
        assert all("id" in f["message"] for f in socket.frames[frames:])
        changed = {**params, "messages": say("other")}
        assert code(await call(backend, "chat.send", changed, request_id=request_id)) == "id_reused"

        # A new backend process reads the same records, events and seqs from durable state.
        requested = len(gateway.event_requests)
        restarted = await backend.restart()
        assert (await events_read(restarted, session_id, request_id))["result"] == read
        reread = await call(
            restarted, "state.get", {"resource": "requests", "id": request_id, "scope": WORKSPACE}
        )
        assert reread["result"] == record
        session = (
            await call(
                restarted,
                "state.get",
                {"resource": "sessions", "id": session_id, "scope": WORKSPACE},
            )
        )["result"]["item"]
        assert [r["request_id"] for r in session["requests"]] == [request_id]
        assert session["active_request_ids"] == []
        assert session["title"] == "hi"
        gateway.titles[session_id] = "Saved Hermes title"
        titled = await call(restarted, "state.list", {"resource": "sessions"})
        assert titled["result"]["items"][0]["title"] == "Saved Hermes title"
        # The log, not the gateway, answers: a gateway restart changes no read.
        gateway.restart()
        assert (await events_read(restarted, session_id, request_id))["result"] == read
        assert all(run != run_id for run, _ in gateway.event_requests[requested:])

    run(system, scenario)
    ctx, hermes, _ = system
    assert [name for name, _ in hermes.calls].count("start") == 1


def test_relayed_reasoning_streams_into_the_log_before_the_tool_and_late_reasoning_is_dropped(
    system,
):
    """The gateway plugin's relay, the backend's relay server and the request's log, as the image
    wires them; only the gateway's run stream is scripted."""
    gateway = Gateway(system[1])

    def script(run_id):
        events = Gateway.script(gateway, run_id)
        # Kimi streams its reasoning before any text; one Session id is a compaction's.
        thinking = [{"think": f"Step {n}. "} for n in range(3)] + [
            {"think": "Then list.", "rotated": True}
        ]
        return events[:1] + thinking + events[2:]

    gateway.script = script

    async def scenario(backend, gateway, ctx, hermes):
        server = await asyncio.start_server(backend.relay, "127.0.0.1", 0)
        relay = Relay(f"127.0.0.1:{server.sockets[0].getsockname()[1]}")

        async def think(session_id, event):
            if event.get("rotated"):
                relay.post_compaction(session_id="api_rotated", old_session_id=session_id)
                session_id = "api_rotated"
            delta = {"delta": event["think"], "kind": "reasoning", "session_id": session_id}
            await asyncio.to_thread(relay.on_stream_delta, **delta)
            # The relayed line is placed before the stream's next event, as when the model
            # thinks before it answers.
            await until(lambda: backend.relayed == relay.sent)
            await until(lambda: all(q.empty() for q in backend.thinking.values()))
            await asyncio.sleep(0.05)

        gateway.think = think
        session_id = await opened(backend)
        request_id = str(uuid4())
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params, request_id=request_id)
        socket = backend.socket
        await until(lambda: socket.notes("chat.finished"))
        read = (await events_read(backend, session_id, request_id))["result"]
        assert [e["kind"] for e in read["events"]] == [
            "reasoning",
            "text.delta",
            "tool.started",
            "tool.completed",
            "text.delta",
            "finished",
        ]
        # Chunks within REASONING_MERGE_SECONDS share one event; seqs stay contiguous.
        assert read["events"][0]["text"] == "Step 0. Step 1. Step 2. Then list."
        assert [cursor(e) for e in read["events"]] == [(0, n) for n in range(1, 7)]
        assert [n["event"] for n in socket.notes("chat.event")] == read["events"]
        # Reasoning relayed after its run ended has no place in any log.
        assert backend.thinking == {}
        await asyncio.to_thread(
            relay.on_stream_delta, delta="Late.", kind="reasoning", session_id=session_id
        )
        await until(lambda: backend.relayed == relay.sent)
        assert (await events_read(backend, session_id, request_id))["result"] == read
        server.close()

    run(system, scenario, gateway=gateway)


def test_a_dropped_run_stream_resumes_from_its_last_sequence_without_duplicates(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        gateway.drop_after = 2
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params)
        await until(lambda: backend.socket.notes("chat.finished"))
        # The consumer resumes after the drop, and the delta merge spans it.
        assert [last for _, last in gateway.event_requests] == ["-1", "2"]
        deltas = [
            c["chunk"]["choices"][0]["delta"]["content"] for c in backend.socket.notes("chat.chunk")
        ]
        assert deltas == ["Hello", "Done"]
        seqs = [n["event"]["seq"] for n in backend.socket.notes("chat.event")]
        assert seqs == list(range(1, 7))

    run(system, scenario)


class SeamFault(httpx.AsyncBaseTransport):
    """The real seam, except that the ``failing``-th matching request gets no answer: it times out
    before reaching the route, or after the route has committed and answered."""

    def __init__(self, method, path, failing, *, committed):
        self.real = httpx.AsyncHTTPTransport()
        self.method, self.path, self.failing, self.committed = method, path, failing, committed
        self.matched = 0
        self.answers: list[int] = []  # the status of each matching request after the fault

    async def handle_async_request(self, request):
        if (request.method, request.url.path) != (self.method, self.path):
            return await self.real.handle_async_request(request)
        self.matched += 1
        if self.matched == self.failing and not self.committed:
            raise httpx.ReadTimeout("timed out before the route")
        response = await self.real.handle_async_request(request)
        if self.matched == self.failing:
            await response.aclose()
            raise httpx.ReadTimeout("timed out after the route committed")
        if self.matched > self.failing:
            self.answers.append(response.status_code)
        return response


@pytest.mark.parametrize("committed", [False, True], ids=["before-commit", "after-commit"])
@pytest.mark.parametrize(
    "failing",
    # The second append is the merged text of a batch, with the tool call still to be sent.
    [1, 2],
    ids=["one-event", "first-of-two-events"],
)
def test_an_append_with_no_answer_ends_the_backend_and_the_next_one_resumes_the_log(
    system, failing, committed
):
    _, hermes, _ = system
    gateway = Gateway(hermes)
    fault = SeamFault("POST", "/message-events", failing, committed=committed)
    request_id = str(uuid4())

    async def send(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params, request_id=request_id)
        await until(lambda: backend.socket.notes("chat.finished"))

    with pytest.raises(ExceptionGroup) as ended:
        run(system, send, seam_transport=fault, gateway=gateway)
    (refusal,) = ended.value.exceptions
    assert isinstance(refusal, aide.Refusal) and refusal.code == "unavailable"
    assert isinstance(refusal.__cause__, httpx.ReadTimeout)
    # It stopped there: no later event reached the log, and the stream was not followed again.
    assert fault.answers == [] and len(gateway.event_requests) == 1

    async def resume(backend, gateway, ctx, hermes):
        (session_id,) = hermes.sessions
        # Model an already subscribed frontend before recovery can push. state.get awaits
        # a scope read before subscribing, so it can otherwise race the recovering consumer.
        subscription = aide.Subscription(aide.Scope.model_validate(WORKSPACE))
        subscription.frontends.add(FRONTEND)
        backend.subscribers[session_id] = subscription
        state = {"resource": "sessions", "id": session_id, "scope": WORKSPACE}
        await call(backend, "state.get", state)
        await until(lambda: backend.socket.notes("chat.finished"))
        read = (await events_read(backend, session_id, request_id))["result"]
        assert [cursor(e) for e in read["events"]] == [(0, n) for n in range(1, 7)]
        assert [e["kind"] for e in read["events"]] == [
            "reasoning",
            "text.delta",
            "tool.started",
            "tool.completed",
            "text.delta",
            "finished",
        ]
        assert read["done"] is True
        # The next backend pushes only what the log did not hold, so a push answered by a timeout
        # stays lost; the frontend reads it with chat.events.
        logged_before = failing if committed else failing - 1
        pushed = [n["event"]["seq"] for n in backend.socket.notes("chat.event")]
        assert pushed == list(range(logged_before + 1, 7))

    run(system, resume, gateway=gateway)
    assert [name for name, _ in hermes.calls].count("start") == 1


def test_a_failed_read_for_a_push_follows_the_stream_again_and_loses_nothing(system):
    """Unlike an append, a read leaves the log's position alone, so the consumer keeps going."""
    fault = SeamFault("GET", "/session-pending", 2, committed=False)

    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params)
        await until(lambda: backend.socket.notes("chat.finished"))
        assert len(gateway.event_requests) == 2
        seqs = [n["event"]["seq"] for n in backend.socket.notes("chat.event")]
        assert seqs == list(range(1, 7))
        messages = backend.socket.notes("chat.message")
        assert [m["message"]["role"] for m in messages] == ["assistant", "tool", "assistant"]

    run(system, scenario, seam_transport=fault)


def test_a_rebuilt_backend_resumes_pushes_for_an_open_request_without_a_new_send(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        gateway.script = lambda run_id: [
            {"event": "run.started"},
            {"event": "message.delta", "delta": "Hel"},
            {"gate": True},
            {"event": "message.delta", "delta": "lo"},
            {"event": "run.completed"},
        ]
        request_id = str(uuid4())
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params, request_id=request_id)
        await until(lambda: backend.socket.notes("chat.chunk"))
        # The live run's stream stays open: a read returns what is retained so far.
        live = (await events_read(backend, session_id, request_id))["result"]
        assert [(e["seq"], e["delta"]) for e in live["events"]] == [(1, "Hel")]
        assert live["done"] is False

        restarted = await backend.restart()
        await until(lambda: list(restarted.consumers) == [request_id])
        # A replayed send attaches to the open request and starts nothing.
        await call(restarted, "chat.send", params, request_id=request_id)
        await asyncio.sleep(0.5)
        gateway.gate.set()
        await until(lambda: restarted.socket.notes("chat.finished"))
        chunks = restarted.socket.notes("chat.chunk")
        assert [(c["cursor"], c["chunk"]["choices"][0]["delta"]["content"]) for c in chunks] == [
            ({"attempt": 0, "seq": 2}, "lo")
        ]
        # The resumed completion keeps its id and counts on from the chunk logged before.
        (run_id,) = hermes.runs
        assert [(c["completion_id"], c["seq"]) for c in chunks] == [(f"{run_id}:1", 2)]
        assert [n["event"]["seq"] for n in restarted.socket.notes("chat.event")] == [2, 3]
        assert restarted.socket.notes("chat.finished")[0]["cursor"] == {"attempt": 0, "seq": 3}

    run(system, scenario)
    assert [name for name, _ in system[1].calls].count("start") == 1


def test_a_seam_call_outlasts_the_longest_lock_wait_of_a_default_quiesce(tmp_path):
    marker = {"held_at": datetime.now(UTC).isoformat(), "max_hold_s": DEFAULT_MAX_HOLD_S}
    (tmp_path / "quiesce.json").write_text(json.dumps(marker))
    seam_wait_s = quiesce_busy_timeout_ms(tmp_path / "store.db") / 1000
    timeouts = []

    def answer(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={})

    async def main():
        async with (
            httpx.AsyncClient(
                base_url="http://seam", transport=httpx.MockTransport(answer)
            ) as seam,
            asyncio.TaskGroup() as work,
        ):
            backend = aide.Backend(seam, seam, Availability(), work)
            await backend.call(seam, "POST", "/message-events", json={})

    asyncio.run(main())
    assert timeouts[0] > seam_wait_s


def wait_for_write_lock(path):
    probe = sqlite3.connect(path, isolation_level=None, timeout=0)
    try:
        while True:
            try:
                probe.execute("BEGIN IMMEDIATE")
                probe.execute("ROLLBACK")
            except sqlite3.OperationalError:
                return
            time.sleep(0.001)
    finally:
        probe.close()


def test_a_default_quiesce_hold_delays_a_running_request_and_loses_nothing(system, monkeypatch):
    """A real ``research store quiesce`` with its default hold, so this takes 30 s on purpose.

    The text and tool call arrive as the lock is taken: SQLite polls its lock up to 100 ms apart, so
    an append this early answers after the whole hold, and the second event of the pair has yet to
    be sent. A client timeout of 30 s loses that event here and ends the backend.
    """
    monkeypatch.setattr(sys.modules[__name__], "SCENARIO_LIMIT_S", DEFAULT_MAX_HOLD_S + 30)
    ctx = system[0]

    async def scenario(backend, gateway, ctx, hermes):
        gateway.script = lambda run_id: [
            {"event": "run.started"},
            {"event": "reasoning.available", "text": "Thinking."},
            {"gate": True},
            {"event": "message.delta", "delta": "Hello"},
            {"event": "tool.started", "tool": "ls", "preview": "ls", "tool_call_id": "call_1"},
            {"event": "tool.completed", "tool": "ls", "tool_call_id": "call_1"},
            {"event": "run.completed"},
        ]
        session_id = await opened(backend)
        request_id = str(uuid4())
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params, request_id=request_id)
        await until(lambda: backend.socket.notes("chat.event"))

        command = [sys.executable, "-m", "research_core.cli", "store", "quiesce"]
        env = {**os.environ, "IDE_WORKSPACE": str(ctx.workspace.root)}
        env.pop("HERMES_HOME", None)  # never a real profile's state.db
        quiesce = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=env)
        try:
            await asyncio.to_thread(wait_for_write_lock, ctx.workspace.store_path)
            gateway.gate.set()
            started = time.monotonic()
            await until(lambda: backend.socket.notes("chat.finished"))
            assert time.monotonic() - started >= DEFAULT_MAX_HOLD_S - 2  # the hold really held it
            assert (await asyncio.to_thread(quiesce.wait)) == 0
        finally:
            if quiesce.poll() is None:
                quiesce.kill()
                quiesce.wait()
        # Every event was logged in order and pushed, none of them after a timeout.
        read = (await events_read(backend, session_id, request_id))["result"]
        pushed = [n["event"] for n in backend.socket.notes("chat.event")]
        assert read["events"] == pushed
        assert [e["kind"] for e in pushed] == [
            "reasoning",
            "text.delta",
            "tool.started",
            "tool.completed",
            "finished",
        ]

    run(system, scenario)
    assert ctx.wstore.one("PRAGMA integrity_check")[0] == "ok"


def test_a_held_request_reads_waiting_then_stitches_its_next_attempt(system):
    ctx, hermes, server = system

    def fail_first(run):
        if len(hermes.runs) == 1:
            hermes.fail(run.run_id, "HTTP 429: slow down")

    async def scenario(backend, gateway, ctx, hermes):
        hermes.script = fail_first
        session_id = await opened(backend)

        def script(run_id):
            if run_id == next(iter(hermes.runs)):
                return [
                    {"event": "run.started"},
                    {"event": "message.delta", "delta": "Wor"},
                    {"event": "run.failed", "error": "HTTP 429: slow down"},
                ]
            return [
                {"event": "run.started"},
                {"event": "message.delta", "delta": "Back"},
                {"event": "run.completed"},
            ]

        gateway.script = script
        request_id = str(uuid4())
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params, request_id=request_id)
        get = {"resource": "requests", "id": request_id, "scope": WORKSPACE}

        async def status():
            return (await call(backend, "state.get", get))["result"]["item"]

        while (await status())["status"] != "waiting":
            await asyncio.sleep(0.05)
        # A new backend reads the hold, its reason and its resume time from the store.
        restarted = await backend.restart()
        held = (await call(restarted, "state.get", get))["result"]["item"]
        assert held["waiting"]["reason"] == "rate_limited" and held["waiting"]["until"]
        assert (held["error"], held["attempt"], held["in_history"]) == (None, 0, True)
        server.test_clock.now += timedelta(minutes=2)
        await until(lambda: restarted.socket.notes("chat.finished"))
        read = (await events_read(restarted, session_id, request_id))["result"]
        assert [(cursor(e), e["kind"]) for e in read["events"]] == [
            ((0, 1), "text.delta"),
            ((1, 1), "text.delta"),
            ((1, 2), "finished"),
        ]
        (chunk,) = restarted.socket.notes("chat.chunk")
        assert chunk["cursor"] == {"attempt": 1, "seq": 1}
        assert (await call(restarted, "state.get", get))["result"]["item"]["attempt"] == 1
        # The first attempt reads from the log after the gateway restarts.
        gateway.restart()
        assert (await events_read(restarted, session_id, request_id))["result"] == read
        current = await events_read(restarted, session_id, request_id, {"attempt": 1, "seq": 0})
        assert current["result"]["events"] == read["events"][1:]

    run(system, scenario)


def test_failed_and_unstarted_requests_stay_readable_after_a_restart(system):
    async def scenario(backend, gateway, ctx, hermes):
        hermes.script = lambda run: hermes.fail(run.run_id, "agent run failed")
        gateway.script = lambda run_id: [{"event": "run.started"}, {"event": "run.failed"}]
        session_id = await opened(backend)
        failed_id = str(uuid4())
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params, request_id=failed_id)
        await until(lambda: backend.socket.notes("chat.finished"))
        settings.update(ctx.wstore, HARNESS, "paused", True, cites="hold dispatch")
        queued_id = str(uuid4())
        queued = {"session_id": session_id, "messages": say("later"), "scope": WORKSPACE}
        await call(backend, "chat.send", queued, request_id=queued_id)
        cancel = {"session_id": session_id, "request_id": queued_id, "scope": WORKSPACE}
        assert (await call(backend, "chat.cancel", cancel))["result"] == {"requested": True}
        await until(lambda: len(backend.socket.notes("chat.finished")) == 2)

        restarted = await backend.restart()
        listing = (
            await call(
                restarted,
                "state.list",
                {"resource": "requests", "session_id": session_id, "scope": WORKSPACE},
            )
        )["result"]
        assert [(i["request_id"], i["status"]) for i in listing["items"]] == [
            (queued_id, "cancelled"),
            (failed_id, "failed"),
        ]
        failed, cancelled = listing["items"][1], listing["items"][0]
        assert failed["error"] == {"class": None, "code": "failed", "message": "agent run failed"}
        assert (cancelled["error"], cancelled["in_history"]) == (None, False)
        assert "messages" not in cancelled
        got = await call(
            restarted, "state.get", {"resource": "requests", "id": queued_id, "scope": WORKSPACE}
        )
        assert got["result"]["item"]["messages"] == say("later")
        outcome = (await events_read(restarted, session_id, failed_id))["result"]["events"]
        assert [(e["kind"], e["status"], e["error"]["code"]) for e in outcome] == [
            ("finished", "failed", "failed")
        ]
        never = (await events_read(restarted, session_id, queued_id))["result"]
        assert [(cursor(e), e["status"]) for e in never["events"]] == [((0, 1), "cancelled")]
        assert (
            code(
                await call(
                    restarted,
                    "state.get",
                    {"resource": "requests", "id": queued_id, "scope": LEAGUE},
                )
            )
            == "forbidden"
        )

    run(system, scenario)


def midway(run_id):
    return [
        {"event": "run.started"},
        {"event": "reasoning.available", "text": "Thinking."},
        {"event": "message.delta", "delta": "Hel"},
        {"event": "message.delta", "delta": "lo"},
        {"event": "tool.started", "tool": "ls", "preview": "ls", "tool_call_id": "call_1"},
        {"gate": True},
        {"event": "tool.completed", "tool": "ls", "tool_call_id": "call_1"},
        {"event": "message.delta", "delta": "Done"},
        {"event": "run.completed"},
    ]


def started(backend):
    return any(n["event"]["kind"] == "tool.started" for n in backend.socket.notes("chat.event"))


def test_a_backend_restart_mid_run_keeps_every_logged_event_and_finishes_the_run(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        # Hermes keeps only a few events per run, so the start of the run is gone from it.
        gateway.capacity, gateway.script = 3, midway
        request_id = str(uuid4())
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params, request_id=request_id)
        await until(lambda: started(backend))
        before = [n["event"] for n in backend.socket.notes("chat.event")]

        restarted = await backend.restart()
        live = (await events_read(restarted, session_id, request_id))["result"]
        assert live["events"] == before and live["done"] is False
        assert [e["kind"] for e in before] == ["reasoning", "text.delta", "tool.started"]
        gateway.gate.set()
        await until(lambda: restarted.socket.notes("chat.finished"))
        after = [n["event"] for n in restarted.socket.notes("chat.event")]
        read = (await events_read(restarted, session_id, request_id))["result"]
        assert read["events"] == before + after and read["done"] is True
        assert [cursor(e) for e in read["events"]] == [(0, n) for n in range(1, 7)]
        assert [e["kind"] for e in after] == ["tool.completed", "text.delta", "finished"]
        assert after[-1]["status"] == "completed"

    run(system, scenario)
    assert [name for name, _ in system[1].calls].count("start") == 1


def test_a_run_gone_after_a_full_restart_ends_interrupted_with_its_events_readable(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        gateway.script = midway
        request_id = str(uuid4())
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params, request_id=request_id)
        await until(lambda: started(backend))
        before = [n["event"] for n in backend.socket.notes("chat.event")]
        # The whole runtime restarts: Hermes loses the run and its buffer with it.
        (run_id,) = hermes.runs
        del hermes.runs[run_id]
        gateway.restart()
        restarted = await backend.restart()
        # The reloaded frontend subscribes before the new backend's first await.
        state = {"resource": "sessions", "id": session_id, "scope": WORKSPACE}
        await call(restarted, "state.get", state)
        await until(lambda: restarted.socket.notes("chat.finished"))
        (finished,) = restarted.socket.notes("chat.finished")
        assert finished["status"] == "interrupted" and finished["error"]["code"] == "interrupted"
        assert finished["cursor"] == {"attempt": 0, "seq": 4}
        read = (await events_read(restarted, session_id, request_id))["result"]
        assert read["events"][:3] == before and read["done"] is True
        (outcome,) = [n["event"] for n in restarted.socket.notes("chat.event")]
        assert read["events"][3] == outcome
        assert (outcome["kind"], outcome["status"]) == ("finished", "interrupted")
        assert outcome["error"]["message"]
        # A frontend that applied the pushed events reads the outcome right after them.
        tail = await events_read(restarted, session_id, request_id, {"attempt": 0, "seq": 3})
        assert tail["result"]["events"] == [outcome]
        assert tail["result"]["next"] == finished["cursor"]

    run(system, scenario)


def test_a_steered_follow_up_logs_only_its_outcome_and_leaves_the_host_run_alone(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        script = gateway.script
        gateway.script = lambda run_id: [*script(run_id)[:5], {"gate": True}, *script(run_id)[5:]]
        first, follow_up = str(uuid4()), str(uuid4())
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params, request_id=first)
        await until(lambda: started(backend))
        await call(backend, "chat.send", params | {"messages": say("also")}, request_id=follow_up)
        (run_id,) = hermes.runs
        await until(lambda: hermes.steered.get(run_id) == ["also"])
        wrapped = (
            "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered "
            "once at this position; not tool output and not a new delivery when replayed "
            "from conversation history]\nalso\n[/OUT-OF-BAND USER MESSAGE]"
        )
        gateway.say(session_id, role="user", content=wrapped, display_kind="steer")
        stored = gateway.transcripts[session_id][-1]
        gateway.gate.set()
        await until(lambda: len(backend.socket.notes("chat.finished")) == 2)
        finished = {f["request_id"]: f["status"] for f in backend.socket.notes("chat.finished")}
        assert finished == {first: "completed", follow_up: "completed"}
        hosted = (await events_read(backend, session_id, first))["result"]["events"]
        assert [e["kind"] for e in hosted] == [
            "reasoning",
            "text.delta",
            "tool.started",
            "tool.completed",
            "text.delta",
            "finished",
        ]
        # The host run's stream is the first request's; the follow-up's log holds its outcome.
        (outcome,) = (await events_read(backend, session_id, follow_up))["result"]["events"]
        assert (outcome["kind"], cursor(outcome)) == ("finished", (0, 1))
        pushed = {n["request_id"] for n in backend.socket.notes("chat.event")}
        assert pushed == {first, follow_up}
        assert len(backend.socket.notes("chat.event")) == len(hosted) + 1
        # The host run's messages are announced once, under the request that started it.
        messages = backend.socket.notes("chat.message")
        assert messages and {m["request_id"] for m in messages} == {first}
        (steer,) = [m for m in messages if m["sequence"] == stored["id"]]
        assert steer["message"] == {"role": "user", "content": "also"}
        history = await call(
            backend, "chat.history", {"session_id": session_id, "scope": WORKSPACE}
        )
        (saved,) = [m for m in history["result"]["items"] if m["id"] == steer["id"]]
        assert saved["message"] == steer["message"]
        assert stored["content"] == wrapped

    run(system, scenario)


def test_a_seam_error_while_logging_ends_the_backend_and_a_restart_resumes_the_log(system):
    gateways = []

    async def failing(backend, gateway, ctx, hermes):
        gateways.append(gateway)
        session_id = await opened(backend)
        request = backend.seam.request

        async def busy(method, path, **kwargs):
            # The seam times out on the second event of the log; the gateway stream is healthy.
            if (
                path == "/message-events"
                and method == "POST"
                and kwargs["json"]["event"]["seq"] == 2
            ):
                raise httpx.ReadTimeout("seam busy")
            return await request(method, path, **kwargs)

        backend.seam.request = busy
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params)
        await asyncio.Event().wait()  # until the consumer's failure ends the backend

    with pytest.raises(ExceptionGroup) as ended:
        run(system, failing)
    (refusal,) = ended.value.exceptions
    assert isinstance(refusal, aide.Refusal) and str(refusal) == "ReadTimeout"
    assert isinstance(refusal.__cause__, httpx.ReadTimeout)
    # Not taken for a gateway drop: the run's stream was opened once and nothing was skipped.
    ((_, last_seq),) = gateways[0].event_requests
    assert last_seq == "-1"

    async def restarted(backend, gateway, ctx, hermes):
        # The container restarts: a new seam client, gateway and backend, the same store.
        request_id, session_id = ctx.wstore.one(
            "SELECT op_id,session_id FROM client_admissions WHERE kind='message'"
        )
        await until(lambda: request_id in backend.finished)
        read = (await events_read(backend, session_id, request_id))["result"]
        assert [cursor(e) for e in read["events"]] == [(0, n) for n in range(1, 7)]
        assert read["events"][-1]["status"] == "completed"

    run(system, restarted)


def test_a_relay_rebind_ends_the_last_bindings_subscriptions(system):
    from websockets.asyncio.server import serve as websocket_server

    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        assert backend.subscribers[session_id].frontends == {FRONTEND}
        binding = {"backend_id": str(uuid4()), "version": "1.0.0", "max_message_bytes": 1048576}

        async def relay(socket):
            await socket.send(json.dumps({"binding": binding}))

        async with websocket_server(
            relay, "127.0.0.1", 0, select_subprotocol=lambda _, offered: offered[0]
        ) as server:
            await backend.connect(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", "proof")
        assert backend.subscribers == {}

    run(system, scenario)


def test_only_a_chat_cancel_ends_a_running_request_cancelled(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        gateway.script = lambda run_id: [
            {"event": "run.started"},
            {"gate": True},
            {"event": "run.cancelled"},
        ]
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        outcomes = {}
        for how in ("workspace stop", "chat.cancel"):
            request_id = str(uuid4())
            await call(backend, "chat.send", params, request_id=request_id)
            await until(lambda: len(hermes.runs) == len(outcomes) + 1)
            run_id = list(hermes.runs)[-1]
            if how == "chat.cancel":
                cancel = {"session_id": session_id, "request_id": request_id, "scope": WORKSPACE}
                assert (await call(backend, "chat.cancel", cancel))["result"] == {"requested": True}
            else:
                # A drain or shutdown stops the run natively; nobody cancelled the request.
                hermes.stop(run_id, profile="orchestrator")
            gateway.gate.set()
            await until(lambda: len(backend.socket.notes("chat.finished")) == len(outcomes) + 1)
            gateway.gate = asyncio.Event()
            got = await call(
                backend, "state.get", {"resource": "requests", "id": request_id, "scope": WORKSPACE}
            )
            outcomes[how] = got["result"]["item"]
        stopped, cancelled = outcomes["workspace stop"], outcomes["chat.cancel"]
        assert stopped["status"] == "interrupted"
        assert stopped["error"] == {
            "class": None,
            "code": "interrupted",
            "message": "The workspace stopped the run",
        }
        assert (cancelled["status"], cancelled["error"]) == ("cancelled", None)
        finished = backend.socket.notes("chat.finished")
        assert [(f["status"], f.get("error")) for f in finished] == [
            ("interrupted", stopped["error"]),
            ("cancelled", None),
        ]

    run(system, scenario)


def test_chat_send_forwards_optional_context_and_instructions_to_the_admission(system):
    view = {"id": "league-standings", "label": "League standings"}
    sent = {}

    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        plain = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        sent["plain"] = (await call(backend, "chat.send", plain))["result"]["request_id"]
        await until(lambda: backend.socket.notes("chat.finished"))
        rich = {
            **plain,
            "context": {"contract_version": 1, "view": view},
            "instructions": "Answer briefly.",
        }
        sent["rich"] = (await call(backend, "chat.send", rich))["result"]["request_id"]

    run(system, scenario)
    ctx = system[0]

    def payload(op_id):
        row = ctx.wstore.one("SELECT payload FROM client_admissions WHERE op_id=?", (op_id,))
        return json.loads(row["payload"])

    plain, rich = payload(sent["plain"]), payload(sent["rich"])
    assert "view" not in plain["context"] and plain["instructions"] is None
    assert rich["context"]["view"] == view and rich["instructions"] == "Answer briefly."


def test_session_open_stamps_the_named_client_and_send_refuses_typed_inference(system):
    class Blocked:
        blocked, until = "budget_exhausted", datetime(2026, 11, 6, tzinfo=UTC)

    class Closed(Availability):
        def available(self):
            return False

        def blocking(self):
            return Blocked()

    async def scenario(backend, gateway, ctx, hermes):
        client = {"id": "softmax-play", "kind": "web", "guide": "client-play"}
        reply = await call(backend, "session.open", {"scope": WORKSPACE, "client": client})
        session_id = reply["result"]["session"]["id"]
        assert system[2]._clients[session_id] == client
        default = await opened(backend)
        assert system[2]._clients[default] == {"id": "aide", "kind": "ide"}
        refused = await call(
            backend,
            "chat.send",
            {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE},
        )
        assert refused["error"]["data"] == {
            "code": "inference_unavailable",
            "reason": "budget_exhausted",
            "until": "2026-11-06T00:00:00+00:00",
        }
        reads = {"session_id": session_id, "scope": WORKSPACE, "wake": False}
        assert "result" in await call(backend, "chat.history", reads)

    run(system, scenario, inference=Closed())


def test_an_aide_open_with_no_client_is_recorded_unknown_and_cannot_write_identity(system):
    async def scenario(backend, gateway, ctx, hermes):
        anonymous = await opened(backend)  # the frontend names no client
        named = await call(
            backend,
            "session.open",
            {"scope": WORKSPACE, "client": {"id": "aide", "kind": "ide"}},
        )
        named_id = named["result"]["session"]["id"]
        assert system[2]._clients[anonymous] == {"id": "aide", "kind": "ide"}
        row = sessions.read(ctx.wstore, anonymous)
        assert row["client"] == sessions.UNKNOWN_CLIENT
        assert not sessions.opened_by_human_chat(row)
        assert sessions.opened_by_human_chat(sessions.read(ctx.wstore, named_id))

    run(system, scenario)


def test_scope_mismatches_are_forbidden_and_unscoped_sessions_are_hidden(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        coworld_id = await opened(backend, LEAGUE)
        other_league = {**LEAGUE, "league": "league_2"}
        other_session = await opened(backend, other_league)
        # A Coworld session without a league has no AIDE scope.
        hidden = await backend.seam.post(
            "/session-open",
            json={
                "op_id": str(uuid4()),
                "client": {"id": "legacy", "kind": "ide"},
                "session_context": {
                    "contract_version": 1,
                    "scope": "coworld",
                    "coworld": COWORLD_ID,
                },
            },
        )
        hidden_id = hidden.json()["session_id"]
        for method, params in (
            ("chat.send", {"session_id": session_id, "messages": say("x"), "scope": LEAGUE}),
            ("chat.history", {"session_id": session_id, "scope": LEAGUE}),
            ("state.get", {"resource": "sessions", "id": coworld_id, "scope": WORKSPACE}),
            ("state.get", {"resource": "sessions", "id": hidden_id, "scope": LEAGUE}),
        ):
            assert code(await call(backend, method, params)) == "forbidden", method
        listed = await call(backend, "state.list", {"resource": "sessions"})
        assert {s["id"] for s in listed["result"]["items"]} == {
            session_id,
            coworld_id,
            other_session,
        }
        scoped = await call(backend, "state.list", {"resource": "sessions", "scope": LEAGUE})
        assert [s["id"] for s in scoped["result"]["items"]] == [coworld_id]
        filters = {"resource": "sessions", "scope_id": COWORLD_ID, "limit": 1}
        first = (await call(backend, "state.list", filters))["result"]
        assert first["next_cursor"]
        second = (await call(backend, "state.list", {**filters, "cursor": first["next_cursor"]}))[
            "result"
        ]
        assert {s["id"]: s["scope"]["league"] for s in first["items"] + second["items"]} == {
            coworld_id: LEAGUE["league"],
            other_session: other_league["league"],
        }
        assert second["next_cursor"] is None
        missing = await call(backend, "state.list", {**filters, "scope_id": "different-coworld"})
        assert missing["result"]["items"] == []
        bad = {"session_id": session_id, "messages": say("a") + say("b"), "scope": WORKSPACE}
        assert code(await call(backend, "chat.send", bad)) == "unsupported"
        unscoped = await call(
            backend, "state.list", {"resource": "requests", "session_id": session_id}
        )
        assert unscoped["error"]["code"] == -32602

    run(system, scenario)


def test_page_offers_and_resolves_a_different_coworld_without_changing_the_source(system):
    async def scenario(backend, gateway, ctx, hermes):
        source = await opened(backend, LEAGUE)
        previous = {"scope": "coworld", "coworld": "cow_other_previous", "league": "league_other"}
        target = await opened(backend, previous)
        page = {**previous, "coworld": "cow_other"}
        params = {
            "session_id": source,
            "scope": LEAGUE,
            "page": page,
            "related_uploads": [previous["coworld"]],
        }
        same_game = await call(
            backend,
            "session.page",
            {
                **params,
                "page": {**page, "league": LEAGUE["league"]},
                "related_uploads": [COWORLD_ID],
            },
        )
        assert same_game["result"] == {"kind": "stay"}
        other_league = await call(
            backend, "session.page", {**params, "page": {**page, "coworld": COWORLD_ID}}
        )
        assert other_league["result"]["kind"] == "offer"
        offer = await call(backend, "session.page", params)
        assert offer["result"] == {"kind": "offer", "target_session_id": target}
        confirmed = await call(backend, "session.page", {**params, "target_session_id": target})
        assert confirmed["result"]["session"]["id"] == target
        assert confirmed["result"]["session"]["scope"] == previous
        assert (
            code(await call(backend, "session.page", {**params, "target_session_id": source}))
            == "not_found"
        )
        still_open = await call(
            backend, "state.get", {"resource": "sessions", "id": source, "scope": LEAGUE}
        )
        assert still_open["result"]["item"]["scope"] == LEAGUE

        empty = {**params, "page": {**page, "league": "league_empty"}}
        assert (await call(backend, "session.page", empty))["result"] == {
            "kind": "offer",
            "target_session_id": None,
        }
        create = {**empty, "target_session_id": None}
        first, retry = await asyncio.gather(
            call(backend, "session.page", create), call(backend, "session.page", create)
        )
        assert first["result"]["session"]["scope"] == empty["page"]
        assert first["result"]["session"]["id"] == retry["result"]["session"]["id"]

    run(system, scenario)


def test_a_wrong_scope_is_refused_to_that_request_alone_and_is_not_recorded(system):
    """A wrong scope on a session no frontend subscribed to yet must not become the scope the
    event watcher reads with: its refusal there would end the backend and recycle the runtime."""

    async def scenario(backend, gateway, ctx, hermes):
        request_id = str(uuid4())
        wrong_scope_requests = (
            ("chat.send", {"messages": say("x")}),
            ("chat.history", {}),
            ("chat.events", {"request_id": request_id, "after": {"attempt": 0, "seq": 0}}),
            ("chat.cancel", {"request_id": request_id}),
            ("state.list", {"resource": "requests"}),
            (
                "input.respond",
                {"request_id": request_id, "response": {"kind": "clarification", "answer": "x"}},
            ),
        )
        ids = [await opened(backend) for _ in wrong_scope_requests]
        read_only_by_get = await opened(backend)
        backend = await backend.restart()  # nothing is subscribed to any of them now
        for session_id, (method, params) in zip(ids, wrong_scope_requests, strict=True):
            reply = await call(
                backend, method, {"session_id": session_id, "scope": LEAGUE, **params}
            )
            assert code(reply) == "forbidden", method
        get = {"resource": "sessions", "id": read_only_by_get, "scope": LEAGUE}
        assert code(await call(backend, "state.get", get)) == "forbidden"
        assert not ctx.wstore.q("SELECT 1 FROM client_admissions WHERE kind='message'")

        witness = await opened(backend)
        for session_id in [*ids, read_only_by_get, witness]:
            sessions.end(ctx.wstore, HARNESS, session_id)

        # Events arrive in order, so once the last one is pushed the watcher got past the others.
        def witness_ended():
            return any(
                n["item"]["id"] == witness and n["item"]["ended_at"]
                for n in backend.socket.notes("state.changed")
            )

        await until(witness_ended)
        assert {n["item"]["id"] for n in backend.socket.notes("state.changed")} == {witness}

    run(system, scenario)


def test_a_refused_frontend_gets_no_pushes_and_a_missing_session_is_not_recorded(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        wrong = {"resource": "sessions", "id": session_id, "scope": LEAGUE}
        assert code(await call(backend, "state.get", wrong, frontend=OTHER)) == "forbidden"
        missing = {"resource": "sessions", "id": "nope", "scope": WORKSPACE}
        assert code(await call(backend, "state.get", missing)) == "not_found"
        assert "nope" not in backend.subscribers

        sessions.end(ctx.wstore, HARNESS, session_id)
        await until(lambda: backend.socket.notes("state.changed"))
        assert not backend.socket.notes("state.changed", OTHER)

    run(system, scenario)


def test_a_scope_with_no_league_is_refused_where_the_seam_would_accept_it(system):
    """The seam compares Coworld and league only, and a Coworld session without a league has no
    AIDE scope: projecting one fails validation, which would end the backend."""
    no_league = {"scope": "coworld", "coworld": COWORLD_ID, "league": None}

    async def scenario(backend, gateway, ctx, hermes):
        hidden = await backend.seam.post(
            "/session-open",
            json={
                "op_id": str(uuid4()),
                "client": {"id": "legacy", "kind": "ide"},
                "session_context": {
                    "contract_version": 1,
                    "scope": "coworld",
                    "coworld": COWORLD_ID,
                },
            },
        )
        hidden_id = hidden.json()["session_id"]
        session_id = await opened(backend)
        for method, params in (
            ("state.get", {"resource": "sessions", "id": hidden_id, "scope": no_league}),
            ("chat.history", {"session_id": hidden_id, "scope": no_league}),
            (
                "state.get",
                {
                    "resource": "sessions",
                    "id": session_id,
                    "scope": {"scope": "coworld", "coworld": None, "league": None},
                },
            ),
        ):
            assert code(await call(backend, method, params)) == "forbidden", method
        assert hidden_id not in backend.subscribers

    run(system, scenario)


class BeforeSessionRead(httpx.AsyncBaseTransport):
    """Run ``hook`` once before the selected reader's next session read."""

    def __init__(self):
        self.real = httpx.AsyncHTTPTransport()
        self.hook = None
        self.reader = None

    async def handle_async_request(self, request):
        if (
            self.hook
            and asyncio.current_task() is self.reader
            and request.url.path == "/snapshot"
            and "session_id" in request.url.params
        ):
            hook, self.hook = self.hook, None
            await hook()
        return await self.real.handle_async_request(request)


def test_a_change_made_while_a_request_read_subscribes_is_in_its_reply(system):
    """A change the watcher consumes before the subscription begins gets no push, so the reply
    has to hold it."""
    transport = BeforeSessionRead()

    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        settings.update(ctx.wstore, HARNESS, "paused", True, cites="hold dispatch")
        request_id = str(uuid4())
        params = {"session_id": session_id, "messages": say("later"), "scope": WORKSPACE}
        await call(backend, "chat.send", params, request_id=request_id)
        backend = await backend.restart()  # nothing is subscribed to the session now

        async def cancel_and_let_the_watcher_consume_it():
            body = {"op_id": request_id, "actor": "user:aide"}
            assert (await backend.seam.post("/message-cancel", json=body)).is_success
            await asyncio.sleep(0.5)

        transport.reader = asyncio.current_task()
        transport.hook = cancel_and_let_the_watcher_consume_it
        # A recovered request consumer reads the same session concurrently.
        await asyncio.create_task(
            backend.scoped(aide.Scope.model_validate(WORKSPACE), session_id=session_id)
        )
        assert transport.hook is cancel_and_let_the_watcher_consume_it
        reply = await call(
            backend, "state.get", {"resource": "requests", "id": request_id, "scope": WORKSPACE}
        )
        assert transport.hook is None
        assert reply["result"]["item"]["status"] == "cancelled"

    run(system, scenario, seam_transport=transport)


def test_a_session_open_with_no_league_is_refused_before_anything_is_stored(system):
    no_league = {"scope": "coworld", "coworld": "cow-a", "league": None}

    async def scenario(backend, gateway, ctx, hermes):
        await backend.snapshot()
        before = ctx.wstore.one("SELECT COUNT(*) FROM sessions")[0]
        assert code(await call(backend, "session.open", {"scope": no_league})) == "forbidden"
        assert ctx.wstore.one("SELECT COUNT(*) FROM sessions")[0] == before
        assert not ctx.wstore.q("SELECT 1 FROM client_admissions WHERE kind='open'")
        assert not ctx.wstore.q("SELECT 1 FROM coworlds WHERE coworld_id='cow-a'")

    run(system, scenario)


def test_a_request_whose_session_the_gateway_does_not_have_closes_its_log(system):
    """The packaged lost-receipt fixture: a store session with no native one must not end the
    backend, or the workspace crash-loops on every boot that recovers this request's consumer."""
    server = system[2]

    async def scenario(backend, gateway, ctx, hermes):
        with server._deliver_lock:  # the row reaches the dispatcher only in its lost-receipt form
            sessions.register(
                ctx.wstore, HARNESS, session_id="no-native", profile="orchestrator", kind="chat"
            )
            request_id = admissions.enqueue(
                ctx.wstore,
                HARNESS,
                key="no-native",
                session_id="no-native",
                profile="orchestrator",
                text="never replayed",
                origin="client",
            )
            with ctx.wstore.tx(HARNESS) as tx:
                tx.execute(
                    "UPDATE client_admissions SET dispatch_started=1,resolution_required=1,"
                    "error='Lost receipt' WHERE op_id=?",
                    (request_id,),
                )

        def outcome():
            return ctx.wstore.one(
                "SELECT event FROM request_events WHERE request_id=? AND kind='finished'",
                (request_id,),
            )

        await until(outcome)
        assert json.loads(outcome()["event"])["status"] == "interrupted"
        await until(lambda: not backend.consumers)
        history = await call(
            backend, "chat.history", {"session_id": "no-native", "scope": WORKSPACE, "limit": 5}
        )
        assert history["result"]["items"] == []
        # The next backend finds the log closed and starts nothing for it.
        restarted = await backend.restart()
        await until(lambda: restarted.start.done())
        assert not restarted.consumers

    run(system, scenario)
    assert not [name for name, _ in system[1].calls if name == "start"]


@pytest.mark.parametrize("number", ["\u00b2", "9" * 5000, "-1", "x"])
def test_a_forged_cursor_is_refused_and_the_backend_keeps_answering(system, number):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        selection = aide.StateList(resource="sessions").model_dump_json(
            include={"resource", "scope_id", "scope", "session_id"}
        )
        key = hashlib.sha256(selection.encode()).hexdigest()[:16]
        listed = await call(backend, "state.list", {"resource": "sessions"})
        revision = listed["result"]["revision"]
        forged_listing = {"resource": "sessions", "cursor": f"{revision}:{number}:{key}"}
        assert code(await call(backend, "state.list", forged_listing)) == "snapshot_expired"
        forged_history = {"session_id": session_id, "scope": WORKSPACE, "cursor": f"{number}:1"}
        assert code(await call(backend, "chat.history", forged_history)) == "snapshot_expired"
        assert "result" in await call(backend, "state.list", {"resource": "sessions"})

    run(system, scenario)


@pytest.mark.parametrize(
    ("method", "handler", "params"),
    [
        ("state.list", "list_state", {"resource": "sessions"}),
        ("task.cancel", "cancel_task", {"task_id": "t", "scope": LEAGUE}),
    ],
)
def test_an_unexpected_failure_answers_only_its_request(system, caplog, method, handler, params):
    async def scenario(backend, gateway, ctx, hermes):
        async def broken(*_args):
            raise KeyError("a bug")

        working = getattr(backend, handler)
        setattr(backend, handler, broken)
        reply = await call(backend, method, params)
        setattr(backend, handler, working)
        assert reply["error"] == {"code": -32603, "message": "Internal error"}
        assert f"{method} failed unexpectedly" in caplog.text
        assert "KeyError" in caplog.text
        assert "result" in await call(backend, "state.list", {"resource": "sessions"})

    run(system, scenario)


def test_a_send_that_fails_after_its_commit_still_runs_once_and_finishes(system, caplog):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        real_call = backend.call

        async def fails_after_committing(client, method, path, **kwargs):
            response = await real_call(client, method, path, **kwargs)
            if path == "/message":
                backend.call = real_call
                raise KeyError("a bug after the commit")
            return response

        backend.call = fails_after_committing
        request_id = str(uuid4())
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        failed = await call(backend, "chat.send", params, request_id=request_id)
        assert code(failed) == "unavailable"
        assert failed["error"]["data"]["outcome"] == "unknown"
        assert failed["error"]["message"] == "Internal error"
        assert "KeyError" not in failed["error"]["message"]
        assert "a bug after the commit" not in failed["error"]["message"]
        assert "chat.send failed unexpectedly" in caplog.text

        # The admission is committed, so the event watcher gives the run its consumer.
        await until(lambda: backend.socket.notes("chat.finished"))
        retried = await call(backend, "chat.send", params, request_id=request_id)
        assert retried["result"]["accepted"] is True
        assert len(hermes.runs) == 1
        assert len(backend.socket.notes("chat.finished")) == 1

    run(system, scenario)


def test_history_pages_by_a_stable_boundary_reads_forward_and_bounds_each_reply(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        await until(lambda: session_id in hermes.sessions)
        for text in ("a", "b", "c", "d", "e"):
            gateway.say(session_id, role="assistant", content=text)
        params = {"session_id": session_id, "scope": WORKSPACE, "limit": 2}
        pages, cursor_, arrived = [], None, False
        while True:
            page = (
                await call(
                    backend, "chat.history", params | ({"cursor": cursor_} if cursor_ else {})
                )
            )["result"]
            pages.append([i["message"]["content"] for i in page["items"]])
            cursor_ = page["next_cursor"]
            if not arrived:
                # New messages do not move a boundary already issued.
                gateway.say(session_id, role="assistant", content="new")
                arrived = True
            if cursor_ is None:
                break
        # The newest records first, each page oldest first, no record repeated or skipped.
        assert pages == [["d", "e"], ["b", "c"], ["a"]]
        first = await call(backend, "chat.history", {"session_id": session_id, "scope": WORKSPACE})
        sequences = [i["sequence"] for i in first["result"]["items"]]
        assert sequences == sorted(sequences)
        forward = await call(
            backend,
            "chat.history",
            {"session_id": session_id, "scope": WORKSPACE, "after_sequence": sequences[0]},
        )
        assert [i["sequence"] for i in forward["result"]["items"]] == sequences[1:]
        assert forward["result"]["next_cursor"] is None
        for _ in range(3):
            gateway.say(session_id, role="assistant", content="x" * 400_000)
        page = (
            await call(backend, "chat.history", {"session_id": session_id, "scope": WORKSPACE})
        )["result"]
        assert len(page["items"]) == 2 and page["next_cursor"] is not None
        gateway.say(session_id, role="assistant", content="y" * 1_100_000)
        huge = await call(backend, "chat.history", {"session_id": session_id, "scope": WORKSPACE})
        assert code(huge) == "too_large"
        unknown = {"session_id": session_id, "request_id": str(uuid4()), "scope": WORKSPACE}
        assert code(await call(backend, "chat.cancel", unknown)) == "not_found"

    run(system, scenario)


def test_history_marks_continuations_failed_turns_and_reasoning():
    rows = [
        aide.NativeMessage(id=1, role="user", content="hi", client_context={"view": "x"}),
        aide.NativeMessage(id=2, role="user", content=run_failures.continuation("rate_limited")),
        aide.NativeMessage(id=3, role="assistant", content="Sorry", display_kind="failed_turn"),
        aide.NativeMessage(id=4, role="assistant", content="Ok", reasoning_content="Because"),
        aide.NativeMessage(id=5, role="assistant", content="", display_kind="hidden"),
    ]
    records = [aide.record(row, "s") for row in rows]
    assert [r and r.kind for r in records] == [
        "message",
        "continuation",
        "failed_turn",
        "message",
        None,
    ]
    assert records[3].model_dump(mode="json") == {
        "id": "4",
        "session_id": "s",
        "sequence": 4,
        "message": {"role": "assistant", "content": "Ok"},
        "kind": "message",
        "reasoning": "Because",
    }


def test_tasks_goals_cancellation_and_pending_input(system):
    async def scenario(backend, gateway, ctx, hermes):
        goal = goals.create(ctx.store, HARNESS, title="Win", end_state="Top", reason="asked")
        task = tasks.create(
            ctx.store,
            HARNESS,
            type="maintenance",
            title="Fix",
            description="Do",
            goal_id=goal["id"],
        )
        listed = await call(backend, "state.list", {"resource": "tasks", "scope_id": COWORLD_ID})
        (item,) = listed["result"]["items"]
        assert item["id"] == f"{COWORLD_ID}:{task['id']}" and item["status"] == "pending"
        assert item["goal_id"] == f"{COWORLD_ID}:{goal['id']}"
        got = await call(
            backend,
            "state.get",
            {"resource": "goals", "id": f"{COWORLD_ID}:{goal['id']}", "scope": LEAGUE},
        )
        assert got["result"]["item"]["status"] == "active"
        assert code(await call(backend, "state.list", {"resource": "goals"})) == "unsupported"
        cancel = {"task_id": item["id"], "scope": LEAGUE}
        assert (await call(backend, "task.cancel", cancel))["result"] == {"requested": True}
        assert (await call(backend, "task.cancel", cancel))["result"] == {"requested": False}
        assert (
            code(await call(backend, "task.cancel", {**cancel, "scope": WORKSPACE})) == "forbidden"
        )
        unregistered = {**LEAGUE, "coworld": "cow_unregistered"}
        stray = {"task_id": "cow_unregistered:T-1", "scope": unregistered}
        assert code(await call(backend, "task.cancel", stray)) == "not_found"

        session_id = await opened(backend)
        hermes.script = lambda run: gateway.controls.__setitem__(
            run.run_id,
            [
                {
                    "kind": "approval",
                    "prompt_id": "p1",
                    "execution_generation": 3,
                    "description": "Delete the scratch folder",
                    "command": "rm -rf scratch",
                    "choices": ["once", "session", "always", "deny"],
                }
            ],
        )
        gateway.script = lambda run_id: [{"event": "run.started"}, {"gate": True}]
        params = {"session_id": session_id, "messages": say("go"), "scope": WORKSPACE}
        await call(backend, "chat.send", params)
        state = {"resource": "sessions", "id": session_id, "scope": WORKSPACE}
        await until(lambda: hermes.runs)
        while not (await call(backend, "state.get", state))["result"]["item"]["pending"]:
            await asyncio.sleep(0.05)
        (pending,) = (await call(backend, "state.get", state))["result"]["item"]["pending"]
        (run_id,) = hermes.runs
        expected = uuid5(NAMESPACE_URL, f"aide-input:{session_id}:{run_id}:3:p1")
        assert pending == {
            "request_id": str(expected),
            "prompt": "Delete the scratch folder",
            "kind": "approval",
            "choices": ["once", "deny"],
            "description": "Delete the scratch folder",
            "command": "rm -rf scratch",
        }
        answer = {"session_id": session_id, "request_id": pending["request_id"], "scope": WORKSPACE}
        wrong = await call(
            backend,
            "input.respond",
            answer | {"response": {"kind": "clarification", "answer": "x"}},
        )
        assert code(wrong) == "conflict"
        once = answer | {"response": {"kind": "approval", "choice": "once"}}
        respond_id = str(uuid4())
        applied = await call(backend, "input.respond", once, request_id=respond_id)
        assert applied["result"] == {"applied": True}
        # An identical retry replays the result, also from a rebuilt backend, without answering
        # the resolved prompt again; other input under that id is refused.
        again = await call(backend, "input.respond", once, request_id=respond_id)
        assert again["result"] == {"applied": True}
        backend = await backend.restart()
        again = await call(backend, "input.respond", once, request_id=respond_id)
        assert again["result"] == {"applied": True}
        deny = answer | {"response": {"kind": "approval", "choice": "deny"}}
        reused = await call(backend, "input.respond", deny, request_id=respond_id)
        assert code(reused) == "id_reused"
        assert gateway.answers == [
            ("approval", {"request_id": "p1", "execution_generation": 3, "choice": "once"})
        ]
        stale = await call(
            backend, "input.respond", answer | {"response": {"kind": "approval", "choice": "deny"}}
        )
        assert code(stale) == "conflict"
        gateway.gate.set()

    run(system, scenario)


def test_scoped_board_changes_reach_only_subscribed_frontends(system):
    async def scenario(backend, gateway, ctx, hermes):
        goal = goals.create(ctx.store, HARNESS, title="Win", end_state="Top", reason="asked")
        task = tasks.create(ctx.store, HARNESS, type="maintenance", title="Fix", description="Do")
        task_params = {"resource": "tasks", "scope_id": COWORLD_ID}
        goal_params = {"resource": "goals", "scope_id": COWORLD_ID}
        task_snapshot = (await call(backend, "state.list", task_params))["result"]
        goal_snapshot = (await call(backend, "state.list", goal_params))["result"]
        await call(backend, "state.list", task_params, frontend=OTHER)

        tasks.update(ctx.store, HARNESS, task["id"], title="Fix the new failure")
        goals.update(ctx.store, HARNESS, int(goal["id"][2:]), title="Win the next round")
        await until(lambda: len(backend.socket.notes("state.changed")) == 2)
        changed_task, changed_goal = backend.socket.notes("state.changed")
        assert changed_task["item"]["title"] == "Fix the new failure"
        assert changed_task["previous_revision"] == task_snapshot["revision"]
        assert changed_goal["item"]["title"] == "Win the next round"
        assert changed_goal["previous_revision"] == goal_snapshot["revision"]
        assert backend.socket.notes("state.changed", OTHER) == [changed_task]

        backend.closed(OTHER)
        tasks.update(ctx.store, HARNESS, task["id"], title="Fix the remaining failure")
        await until(lambda: len(backend.socket.notes("state.changed")) == 3)
        assert (
            backend.socket.notes("state.changed")[-1]["item"]["title"]
            == "Fix the remaining failure"
        )
        assert backend.socket.notes("state.changed", OTHER) == [changed_task]

    run(system, scenario)


def test_pending_clarification_has_no_choices_field(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        hermes.script = lambda run: gateway.controls.__setitem__(
            run.run_id,
            [
                {
                    "kind": "clarify",
                    "prompt_id": "q1",
                    "execution_generation": 1,
                    "question": "Which league?",
                }
            ],
        )
        gateway.script = lambda run_id: [{"event": "run.started"}, {"gate": True}]
        params = {"session_id": session_id, "messages": say("go"), "scope": WORKSPACE}
        await call(backend, "chat.send", params)
        state = {"resource": "sessions", "id": session_id, "scope": WORKSPACE}
        await until(lambda: hermes.runs)
        while not (await call(backend, "state.get", state))["result"]["item"]["pending"]:
            await asyncio.sleep(0.05)
        (pending,) = (await call(backend, "state.get", state))["result"]["item"]["pending"]
        (run_id,) = hermes.runs
        # The contract's clarification variant forbids extra properties, including a null `choices`.
        assert pending == {
            "request_id": str(uuid5(NAMESPACE_URL, f"aide-input:{session_id}:{run_id}:1:q1")),
            "prompt": "Which league?",
            "kind": "clarification",
        }
        gateway.gate.set()

    run(system, scenario)


def test_frontend_closed_ends_its_subscriptions(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        await call(
            backend,
            "state.get",
            {"resource": "sessions", "id": session_id, "scope": WORKSPACE},
            frontend=OTHER,
        )
        backend.closed(OTHER)
        assert backend.subscribers[session_id].frontends == {FRONTEND}

    run(system, scenario)


def test_relay_failures_reconnect_without_touching_the_control_connection(monkeypatch):
    from control import serve as control_serve
    from websockets.asyncio.server import serve as websocket_server

    # No seam runs here, and an unreachable seam ends the backend.
    monkeypatch.setattr(aide.Backend, "watch", lambda self: asyncio.Event().wait())

    async def main():
        attempts, control_connections, bound = [], [], asyncio.Event()
        binding = {"backend_id": str(uuid4()), "version": "1.0.0", "max_message_bytes": 1048576}

        async def platform(socket):
            if socket.request.path.endswith("/connection"):
                control_connections.append(socket)
                await socket.wait_closed()
                return
            attempts.append(socket.request.headers["Authorization"])
            if len(attempts) == 1:
                await socket.close(code=1008)  # superseded or expired: reconnect
                return
            assert socket.subprotocol == "aide.1.0.0"
            await socket.send(json.dumps({"binding": binding}))
            bound.set()
            await socket.wait_closed()

        async with websocket_server(
            platform, "127.0.0.1", 0, select_subprotocol=lambda _, offered: (offered or [None])[0]
        ) as server:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            common = {"platform_url": url, "workspace_id": "w", "credential": "proof"}
            env = {"API_SERVER_KEY": "proof", "IDE_REASONING_RELAY": "127.0.0.1:0"}
            connected = asyncio.Event()
            control = asyncio.create_task(control_serve(**common, env=env, connected=connected))
            backend = asyncio.create_task(aide.serve(**common, env=env, inference=Availability()))
            async with asyncio.timeout(10):
                await bound.wait()
                await connected.wait()
            assert len(control_connections) == 1
            assert attempts == ["Bearer proof", "Bearer proof"]
            assert not control.done() and not backend.done()
            backend.cancel()
            control.cancel()

    asyncio.run(main())


def test_state_changed_precedes_chat_finished(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        request_id = str(uuid4())
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params, request_id=request_id)
        await until(lambda: backend.socket.notes("chat.finished"))
        methods_and_params = [
            (f["message"]["method"], f["message"]["params"])
            for f in backend.socket.frames
            if f["frontend_id"] == str(FRONTEND) and "method" in f["message"]
        ]
        finished_idx = next(
            i for i, (m, _) in enumerate(methods_and_params) if m == "chat.finished"
        )
        preceding_session_updates = [
            p
            for m, p in methods_and_params[:finished_idx]
            if m == "state.changed" and p["resource"] == "sessions"
        ]
        assert preceding_session_updates
        last_before_finished = preceding_session_updates[-1]["item"]
        assert request_id not in last_before_finished["active_request_ids"]
        embedded = {r["request_id"]: r for r in last_before_finished["requests"]}
        assert embedded[request_id]["status"] == "completed"

    run(system, scenario)


def test_epoch_survives_restart_and_changes_on_store_reset(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend, scope=LEAGUE)
        goals.create(ctx.store, HARNESS, title="Win league", end_state="first", reason="asked")
        task = tasks.create(
            ctx.store,
            HARNESS,
            type="maintenance",
            title="Tune policy",
            description="desc",
        )
        w_epoch = ctx.wstore.get_meta("created")
        c_epoch = ctx.store.get_meta("created")
        assert w_epoch and c_epoch

        s_list = (await call(backend, "state.list", {"resource": "sessions"}))["result"]
        r_list = (
            await call(
                backend,
                "state.list",
                {"resource": "requests", "session_id": session_id, "scope": LEAGUE},
            )
        )["result"]
        t_list = (await call(backend, "state.list", {"resource": "tasks", "scope": LEAGUE}))[
            "result"
        ]
        g_list = (await call(backend, "state.list", {"resource": "goals", "scope": LEAGUE}))[
            "result"
        ]
        assert s_list["epoch"] == r_list["epoch"] == w_epoch
        assert t_list["epoch"] == g_list["epoch"] == c_epoch

        restarted = await backend.restart()
        s_after = (await call(restarted, "state.list", {"resource": "sessions"}))["result"]
        t_after = (await call(restarted, "state.list", {"resource": "tasks", "scope": LEAGUE}))[
            "result"
        ]
        assert s_after["epoch"] == w_epoch
        assert t_after["epoch"] == c_epoch

        # Simulate a store reset changing the store's created epoch.
        ctx.wstore.set_meta("created", "2026-05-01T00:00:00.000Z")
        ctx.wstore.conn.commit()
        ctx.store.set_meta("created", "2026-05-02T00:00:00.000Z")
        ctx.store.conn.commit()
        s_reset = (
            await call(
                restarted,
                "state.get",
                {"resource": "sessions", "id": session_id, "scope": LEAGUE},
            )
        )["result"]
        t_reset = (
            await call(
                restarted,
                "state.get",
                {
                    "resource": "tasks",
                    "id": f"{COWORLD_ID}:{task['id']}",
                    "scope": LEAGUE,
                },
            )
        )["result"]
        assert s_reset["epoch"] == "2026-05-01T00:00:00.000Z"
        assert t_reset["epoch"] == "2026-05-02T00:00:00.000Z"

    run(system, scenario)


def test_tasks_and_goals_carry_monotonic_item_revisions(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend, scope=LEAGUE)
        s_get = (
            await call(
                backend,
                "state.get",
                {"resource": "sessions", "id": session_id, "scope": LEAGUE},
            )
        )["result"]
        assert "revision" not in s_get["item"]
        assert s_get["revision"] >= ctx.wstore.last_seq()

        goal = goals.create(ctx.store, HARNESS, title="Goal v1", end_state="end", reason="asked")
        task = tasks.create(
            ctx.store,
            HARNESS,
            type="maintenance",
            title="Task v1",
            description="desc",
        )
        t_list = (await call(backend, "state.list", {"resource": "tasks", "scope": LEAGUE}))[
            "result"
        ]
        g_list = (await call(backend, "state.list", {"resource": "goals", "scope": LEAGUE}))[
            "result"
        ]
        t_rev_1 = t_list["items"][0]["revision"]
        g_rev_1 = g_list["items"][0]["revision"]
        assert t_rev_1 > g_rev_1 >= 1

        tasks.update(ctx.store, HARNESS, task["id"], title="Task v2")
        await until(lambda: backend.socket.notes("state.changed"))
        t_pushed = [n for n in backend.socket.notes("state.changed") if n["resource"] == "tasks"][
            -1
        ]
        assert t_pushed["item"]["revision"] > t_rev_1
        assert t_pushed["item"]["revision"] == t_pushed["revision"]

        g_get = (
            await call(
                backend,
                "state.get",
                {
                    "resource": "goals",
                    "id": f"{COWORLD_ID}:{goal['id']}",
                    "scope": LEAGUE,
                },
            )
        )["result"]
        assert g_get["item"]["revision"] == g_rev_1

    run(system, scenario)


def test_concurrent_changed_calls_preserve_revision_order(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        real_pending = backend.pending
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0

        async def gated_pending(sid, requests):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                await release_first.wait()
            return await real_pending(sid, requests)

        backend.pending = gated_pending
        first_task = asyncio.create_task(backend.changed(session_id))
        await first_entered.wait()
        # Start a second changed() while the first is paused inside pending(); then create a
        # queued request before releasing the first call so the second call's snapshot sees it.
        second_task = asyncio.create_task(backend.changed(session_id))
        settings.update(ctx.wstore, HARNESS, "paused", True, cites="hold")
        req_id = admissions.enqueue(
            ctx.wstore,
            HARNESS,
            key="c-order",
            session_id=session_id,
            profile="orchestrator",
            text="queued",
            origin="client",
        )
        release_first.set()
        await asyncio.gather(first_task, second_task)

        session_notes = [
            n for n in backend.socket.notes("state.changed") if n["resource"] == "sessions"
        ]
        assert len(session_notes) >= 2
        revs = [n["revision"] for n in session_notes]
        assert revs == sorted(revs) and len(set(revs)) == len(revs)
        assert req_id in session_notes[-1]["item"]["active_request_ids"]

    run(system, scenario)


def test_session_pushes_chain_previous_revision_from_state_list(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        listed = (await call(backend, "state.list", {"resource": "sessions"}))["result"]
        start_idx = len(backend.socket.notes("state.changed"))

        settings.update(ctx.wstore, HARNESS, "paused", True, cites="hold")
        req_1 = str(uuid4())
        await call(
            backend,
            "chat.send",
            {"session_id": session_id, "messages": say("one"), "scope": WORKSPACE},
            request_id=req_1,
        )
        await until(lambda: len(backend.socket.notes("state.changed")) > start_idx)
        await call(
            backend,
            "chat.cancel",
            {"session_id": session_id, "request_id": req_1, "scope": WORKSPACE},
        )
        await until(lambda: len(backend.socket.notes("state.changed")) >= start_idx + 2)

        pushes = [
            n
            for n in backend.socket.notes("state.changed")[start_idx:]
            if n["resource"] == "sessions"
        ]
        first, second = pushes[0], pushes[1]
        assert first["previous_revision"] == listed["revision"]
        assert second["previous_revision"] == first["revision"]
        assert listed["revision"] < first["revision"] < second["revision"]

    run(system, scenario)


def test_oversized_message_returns_too_large(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        oversized_str = {
            "session_id": session_id,
            "messages": [{"role": "user", "content": "a" * 50_001}],
            "scope": WORKSPACE,
        }
        reply_str = await call(backend, "chat.send", oversized_str)
        assert reply_str["error"]["code"] == -32000
        assert code(reply_str) == "too_large"

        oversized_parts = {
            "session_id": session_id,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "b" * 30_000},
                        {"type": "text", "text": "c" * 20_001},
                    ],
                }
            ],
            "scope": WORKSPACE,
        }
        reply_parts = await call(backend, "chat.send", oversized_parts)
        assert reply_parts["error"]["code"] == -32000
        assert code(reply_parts) == "too_large"

    run(system, scenario)


def test_input_respond_disallowed_choice_returns_conflict(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        hermes.script = lambda run_obj: gateway.controls.__setitem__(
            run_obj.run_id,
            [
                {
                    "kind": "approval",
                    "prompt_id": "p-deny-only",
                    "execution_generation": 1,
                    "description": "Deny-only prompt",
                    "command": "echo test",
                    "choices": ["deny"],
                }
            ],
        )
        gateway.script = lambda run_id: [{"event": "run.started"}, {"gate": True}]
        await call(
            backend,
            "chat.send",
            {"session_id": session_id, "messages": say("go"), "scope": WORKSPACE},
        )
        state = {"resource": "sessions", "id": session_id, "scope": WORKSPACE}
        await until(lambda: hermes.runs)
        while not (await call(backend, "state.get", state))["result"]["item"]["pending"]:
            await asyncio.sleep(0.05)
        (pending,) = (await call(backend, "state.get", state))["result"]["item"]["pending"]
        assert pending["choices"] == ["deny"]

        disallowed = await call(
            backend,
            "input.respond",
            {
                "session_id": session_id,
                "request_id": pending["request_id"],
                "scope": WORKSPACE,
                "response": {"kind": "approval", "choice": "once"},
            },
        )
        assert disallowed["error"]["code"] == -32000
        assert code(disallowed) == "conflict"
        gateway.gate.set()

    run(system, scenario)


def test_state_get_and_task_cancel_refusal_codes(system):
    async def scenario(backend, gateway, ctx, hermes):
        goal = goals.create(ctx.store, HARNESS, title="Win", end_state="Top", reason="asked")
        task = tasks.create(
            ctx.store,
            HARNESS,
            type="maintenance",
            title="Fix",
            description="Do",
            goal_id=goal["id"],
        )
        mismatched = await call(
            backend,
            "state.get",
            {"resource": "goals", "id": f"{COWORLD_ID}:{task['id']}", "scope": LEAGUE},
        )
        assert code(mismatched) == "not_found"

        invalid_coworld_scope = {"scope": "coworld", "coworld": "bad/coworld", "league": "l1"}
        invalid_get = await call(
            backend,
            "state.get",
            {"resource": "tasks", "id": "bad/coworld:T-1", "scope": invalid_coworld_scope},
        )
        assert code(invalid_get) == "not_found"

        real_board = backend.board

        async def board_then_abandon(coworld_id, resource, item_id=None):
            result = await real_board(coworld_id, resource, item_id)
            tasks.abandon(
                ctx.store,
                HARNESS,
                task["id"],
                failure={"what_failed": "Already ended", "next_step": "None"},
            )
            backend.board = real_board
            return result

        backend.board = board_then_abandon
        cancelled = await call(
            backend,
            "task.cancel",
            {"task_id": f"{COWORLD_ID}:{task['id']}", "scope": LEAGUE},
        )
        assert cancelled["result"] == {"requested": False}

    run(system, scenario)


def test_seam_500_returns_internal_error_without_traceback(system, monkeypatch):
    server = system[2]

    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        real_snapshot = server.snapshot

        def broken_snapshot(*_args, **_kwargs):
            raise RuntimeError("secret database detail")

        monkeypatch.setattr(server, "snapshot", broken_snapshot)
        raw = await backend.seam.get("/snapshot", params={"store": "workspace", "requests": "1"})
        assert raw.status_code == 500
        body = raw.json()
        assert "traceback" not in body
        assert "secret database detail" not in body["error"]
        assert body["error"] == "Internal server error"

        read_reply = await call(backend, "state.list", {"resource": "sessions"})
        assert read_reply["error"] == {"code": -32603, "message": "Internal error"}
        monkeypatch.setattr(server, "snapshot", real_snapshot)

        def broken_message(*_args, **_kwargs):
            raise RuntimeError("secret commit detail")

        monkeypatch.setattr(server, "message", broken_message)
        cmd_reply = await call(
            backend,
            "chat.send",
            {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE},
        )
        assert code(cmd_reply) == "unavailable"
        assert cmd_reply["error"]["data"]["outcome"] == "unknown"
        assert cmd_reply["error"]["message"] == "Internal error"
        assert "secret commit detail" not in cmd_reply["error"]["message"]

    run(system, scenario)


def test_session_title_preserved_across_open_page_and_state_changed(system):
    async def scenario(backend, gateway, ctx, hermes):
        open_id = str(uuid4())
        target_open = await call(backend, "session.open", {"scope": LEAGUE}, request_id=open_id)
        target_id = target_open["result"]["session"]["id"]
        assert target_open["result"]["session"].get("title") is None

        gateway.titles[target_id] = "Named in Hermes"
        send_id = str(uuid4())
        await call(
            backend,
            "chat.send",
            {"session_id": target_id, "messages": say("hi"), "scope": LEAGUE},
            request_id=send_id,
        )
        await until(lambda: backend.socket.notes("chat.finished"))
        changed_titles = [
            note["item"].get("title")
            for note in backend.socket.notes("state.changed")
            if note["resource"] == "sessions" and note["item"]["id"] == target_id
        ]
        assert changed_titles[-1] == "Named in Hermes"

        other_coworld = "coworld-other"
        other_scope = {"scope": "coworld", "coworld": other_coworld, "league": "league_other"}
        other_id = await opened(backend, other_scope)
        switched = await call(
            backend,
            "session.page",
            {
                "session_id": other_id,
                "scope": other_scope,
                "page": LEAGUE,
                "related_uploads": [],
                "target_session_id": target_id,
            },
        )
        assert switched["result"]["kind"] == "switched"
        assert switched["result"]["session"]["title"] == "Named in Hermes"

        restarted = await backend.restart()
        reopened = await call(restarted, "session.open", {"scope": LEAGUE}, request_id=open_id)
        assert reopened["result"]["session"]["id"] == target_id
        # session.open never waits on Hermes: after a restart the title cache is empty, so the
        # reply carries the first-message fallback and a read carries Hermes's title.
        assert reopened["result"]["session"]["title"] == "hi"
        read = await call(
            restarted, "state.get", {"resource": "sessions", "id": target_id, "scope": LEAGUE}
        )
        assert read["result"]["item"]["title"] == "Named in Hermes"

    run(system, scenario)


def test_chat_history_includes_request_id_matching_live_events_and_publish_isolation(system):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        first = str(uuid4())
        second = str(uuid4())

        def two_turns(run_id: str) -> list[dict]:
            if len(hermes.runs) == 1:
                tool_call = {
                    "id": "call_first",
                    "type": "function",
                    "function": {"name": "ls", "arguments": "{}"},
                }
                return [
                    {"event": "run.started"},
                    {
                        "event": "tool.started",
                        "tool": "ls",
                        "preview": "ls",
                        "tool_call_id": "call_first",
                        "say": [{"role": "assistant", "content": None, "tool_calls": [tool_call]}],
                    },
                    {
                        "event": "tool.completed",
                        "tool": "ls",
                        "tool_call_id": "call_first",
                        "say": [{"role": "tool", "content": "files", "tool_call_id": "call_first"}],
                    },
                    {
                        "event": "run.completed",
                        "say": [{"role": "assistant", "content": "Done"}],
                    },
                ]
            return [
                {"event": "run.started"},
                {"gate": True},
                {"event": "run.completed", "say": [{"role": "assistant", "content": "Second"}]},
            ]

        gateway.script = two_turns
        await call(
            backend,
            "chat.send",
            {"session_id": session_id, "messages": say("first question"), "scope": WORKSPACE},
            request_id=first,
        )
        await until(lambda: hermes.runs)
        gateway.say(session_id, role="user", content="first question")
        first_user_id = gateway.transcripts[session_id][-1]["id"]
        with ctx.wstore.tx(HARNESS) as tx:
            tx.execute(
                "UPDATE client_admissions SET native_message_id=? WHERE op_id=?",
                (first_user_id, first),
            )
        await until(lambda: backend.socket.notes("chat.finished"))

        # Insert a native user message belonging to a second non-steered admission, and verify
        # calling publish() for ``first`` stops before leaking it under ``first``.
        await call(
            backend,
            "chat.send",
            {"session_id": session_id, "messages": say("second question"), "scope": WORKSPACE},
            request_id=second,
        )
        await until(lambda: len(hermes.runs) == 2)
        gateway.say(session_id, role="user", content="second question")
        second_user_id = gateway.transcripts[session_id][-1]["id"]
        with ctx.wstore.tx(HARNESS) as tx:
            tx.execute(
                "UPDATE client_admissions SET native_message_id=? WHERE op_id=?",
                (second_user_id, second),
            )
        after = await backend.publish(first, session_id, 0)
        assert after < second_user_id
        published_for_first = [
            n for n in backend.socket.notes("chat.message") if n["sequence"] == second_user_id
        ]
        assert published_for_first == []

        gateway.gate.set()
        await until(lambda: len(backend.socket.notes("chat.finished")) == 2)
        second_notes = [
            n for n in backend.socket.notes("chat.message") if n["sequence"] == second_user_id
        ]
        assert len(second_notes) == 1 and second_notes[0]["request_id"] == second

        history = await call(
            backend, "chat.history", {"session_id": session_id, "scope": WORKSPACE}
        )
        by_seq = {item["sequence"]: item for item in history["result"]["items"]}
        assert by_seq[first_user_id].get("request_id") == first
        assert by_seq[second_user_id].get("request_id") == second
        tool_call_item = next(
            item
            for item in history["result"]["items"]
            if item["message"].get("role") == "assistant" and item["message"].get("tool_calls")
        )
        tool_result_item = next(
            item for item in history["result"]["items"] if item["message"].get("role") == "tool"
        )
        plain_assistant_item = next(
            item
            for item in history["result"]["items"]
            if item["message"] == {"role": "assistant", "content": "Done"}
        )
        assert tool_call_item.get("request_id") == first
        assert tool_result_item.get("request_id") == first
        # Plain assistant text carries no durable per-message request_id in Option B.
        assert "request_id" not in plain_assistant_item

    run(system, scenario)


def test_cancel_merged_steered_followup_and_waiting_request(system):
    _, hermes, _ = system

    def hold_on_rate_limit(run):
        if len(hermes.runs) == 2:
            hermes.fail(run.run_id, "HTTP 429: slow down")

    async def scenario(backend, gateway, ctx, hermes):
        hermes.script = hold_on_rate_limit
        session_id = await opened(backend)
        base_script = gateway.script
        gateway.script = lambda run_id: (
            [*base_script(run_id)[:5], {"gate": True}, *base_script(run_id)[5:]]
            if len(hermes.runs) == 1
            else [{"event": "run.started"}, {"event": "run.failed", "error": "HTTP 429: slow down"}]
        )
        first, follow_up = str(uuid4()), str(uuid4())
        params = {"session_id": session_id, "messages": say("hi"), "scope": WORKSPACE}
        await call(backend, "chat.send", params, request_id=first)
        await until(lambda: started(backend))
        await call(backend, "chat.send", params | {"messages": say("also")}, request_id=follow_up)
        (run_id,) = hermes.runs
        await until(lambda: hermes.steered.get(run_id) == ["also"])

        # Cancelling the steered follow-up stops the shared host run and settles both requests.
        cancel_reply = await call(
            backend,
            "chat.cancel",
            {"session_id": session_id, "request_id": follow_up, "scope": WORKSPACE},
        )
        assert cancel_reply["result"] == {"requested": True}
        assert any(name == "stop" and args["run_id"] == run_id for name, args in hermes.calls)
        gateway.gate.set()
        await until(lambda: len(backend.socket.notes("chat.finished")) == 2)
        statuses = {n["request_id"]: n["status"] for n in backend.socket.notes("chat.finished")}
        assert statuses == {first: "cancelled", follow_up: "cancelled"}

        # Cancelling a waiting request settles immediately with waiting=None, error=None, and
        # no stale retry/waiting state on the session.
        waiting_id = str(uuid4())
        await call(
            backend,
            "chat.send",
            {"session_id": session_id, "messages": say("retry later"), "scope": WORKSPACE},
            request_id=waiting_id,
        )
        get_waiting = {"resource": "requests", "id": waiting_id, "scope": WORKSPACE}
        while (await call(backend, "state.get", get_waiting))["result"]["item"][
            "status"
        ] != "waiting":
            await asyncio.sleep(0.05)

        waiting_cancel = await call(
            backend,
            "chat.cancel",
            {"session_id": session_id, "request_id": waiting_id, "scope": WORKSPACE},
        )
        assert waiting_cancel["result"] == {"requested": True}
        await until(lambda: len(backend.socket.notes("chat.finished")) == 3)
        finished_note = next(
            n for n in backend.socket.notes("chat.finished") if n["request_id"] == waiting_id
        )
        assert finished_note["status"] == "cancelled"
        assert "error" not in finished_note

        settled_req = (await call(backend, "state.get", get_waiting))["result"]["item"]
        assert settled_req["status"] == "cancelled"
        assert settled_req["waiting"] is None
        assert settled_req["error"] is None
        waiting_row = ctx.wstore.one(
            "SELECT status,resume_after FROM client_admissions WHERE op_id=?", (waiting_id,)
        )
        assert waiting_row["status"] == "cancelled" and waiting_row["resume_after"] is None

        settled_session = (
            await call(
                backend,
                "state.get",
                {"resource": "sessions", "id": session_id, "scope": WORKSPACE},
            )
        )["result"]["item"]
        assert settled_session["active_request_ids"] == []

        frame_methods = [
            (f["message"].get("method"), f["message"].get("params"))
            for f in backend.socket.frames
            if f["frontend_id"] == str(FRONTEND)
        ]
        changed_idx = max(
            i
            for i, (method, params_) in enumerate(frame_methods)
            if method == "state.changed"
            and params_["resource"] == "sessions"
            and params_["item"]["id"] == session_id
            and params_["item"]["active_request_ids"] == []
            and any(
                req["request_id"] == waiting_id and req["status"] == "cancelled"
                for req in params_["item"]["requests"]
            )
        )
        finished_idx = next(
            i
            for i, (method, params_) in enumerate(frame_methods)
            if method == "chat.finished" and params_["request_id"] == waiting_id
        )
        assert changed_idx < finished_idx

    run(system, scenario)


def test_cached_hermes_title_skips_repeat_gateway_gets(system, monkeypatch):
    async def scenario(backend, gateway, ctx, hermes):
        session_id = await opened(backend)
        title_gets = 0
        real_get = backend.gateway.get

        async def counted_get(url, *args, **kwargs):
            nonlocal title_gets
            if url == f"api/sessions/{session_id}":
                title_gets += 1
            return await real_get(url, *args, **kwargs)

        monkeypatch.setattr(backend.gateway, "get", counted_get)

        # Untitled at first: fallback is not cached so Hermes can supply a title later.
        await backend.changed(session_id)
        gateway.titles[session_id] = "Settled Hermes Title"
        await backend.changed(session_id)
        await backend.changed(session_id)
        assert title_gets == 2

        changed_titles = [
            note["item"].get("title")
            for note in backend.socket.notes("state.changed")
            if note["resource"] == "sessions" and note["item"]["id"] == session_id
        ]
        assert changed_titles[-2:] == ["Settled Hermes Title", "Settled Hermes Title"]

    run(system, scenario)
