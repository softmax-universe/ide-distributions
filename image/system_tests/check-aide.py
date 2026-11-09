"""Drive the running workspace as the platform: hosting control and AIDE 1.0.0 side by side.

The container's bootstrap names this script's port as the platform, and the orchestrator's
model points at its scripted OpenRouter mirror, so the round trip needs no credentials: lease,
control status, then session.open -> chat.send -> chunks -> chat.finished -> chat.history over
the AIDE backend socket, then the same request's record and events read again over the socket the
backend opens after the platform drops the first one.
"""

import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import yaml
from aiohttp import WSMsgType, web

PLATFORM_PORT, MODEL_PORT = int(sys.argv[1]), int(sys.argv[2])
WORKSPACE = {"scope": "workspace", "coworld": None, "league": None}
REPLY = ["The ", "workspace ", "answers."]


async def main() -> None:
    control_sockets, backend, done = asyncio.Queue(), asyncio.Queue(), asyncio.Event()

    async def lease(request: web.Request) -> web.Response:
        expires = datetime.now(UTC) + timedelta(hours=1)
        return web.json_response(
            {
                "key": "sk-or-scripted",
                "expires_at": expires.isoformat(),
                "renew_at": expires.isoformat(),
                "allowance_usd": 1,
            }
        )

    async def socket(request: web.Request) -> web.WebSocketResponse:
        aide = request.path.endswith("/aide/1.0.0/backend")
        response = web.WebSocketResponse(protocols=("aide.1.0.0",) if aide else ())
        await response.prepare(request)
        if aide:
            binding = {"backend_id": str(uuid4()), "version": "1.0.0", "max_message_bytes": 1048576}
            await response.send_json({"binding": binding})
        await (backend if aide else control_sockets).put(response)
        await done.wait()  # the scenario reads through its own handle
        return response

    async def completions(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        chunks = [{"role": "assistant", "content": REPLY[0]}] + [{"content": t} for t in REPLY[1:]]
        frames = [
            {
                "id": "scripted",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            for delta in chunks
        ]
        frames[-1]["choices"][0]["finish_reason"] = "stop"
        frames.append(
            {
                **frames[-1],
                "choices": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 3, "total_tokens": 4},
            }
        )
        if not body.get("stream"):
            message = {"role": "assistant", "content": "".join(REPLY)}
            return web.json_response(
                {
                    "id": "scripted",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": body["model"],
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    "usage": frames[-1]["usage"],
                }
            )
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for frame in frames:
            await response.write(f"data: {json.dumps(frame)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    platform = web.Application()
    platform.router.add_put("/v2/workspaces/{workspace}/inference", lease)
    platform.router.add_get("/v2/workspaces/{workspace}/connection", socket)
    platform.router.add_get("/v2/workspaces/{workspace}/aide/1.0.0/backend", socket)
    model = web.Application()
    model.router.add_post("/api/v1/chat/completions", completions)
    runners = [web.AppRunner(platform), web.AppRunner(model)]
    for runner, port in zip(runners, (PLATFORM_PORT, MODEL_PORT), strict=True):
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()

    async with asyncio.timeout(300):
        control = await control_sockets.get()
        await control.send_json(
            {"stream_id": "status", "envelope": {"id": str(uuid4()), "control": "status"}}
        )
        header = await control.receive_json()
        assert header["status"] == 200, header
        print("PASS control connection answers beside the AIDE backend", flush=True)
        # Hermes treats a config base_url under provider openrouter as a mirror (#10622).
        config = Path("/workspace/host/profiles/orchestrator/config.yaml")
        settings = yaml.safe_load(config.read_text())
        settings["model"]["base_url"] = f"http://127.0.0.1:{MODEL_PORT}/api/v1"
        config.write_text(yaml.safe_dump(settings))

        relay = await backend.get()
        frontend = str(uuid4())
        notes: list[dict] = []

        async def request(method: str, params: dict) -> dict:
            request_id = str(uuid4())
            message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            await relay.send_json({"frontend_id": frontend, "message": message})
            while True:
                received = await relay.receive()
                assert received.type == WSMsgType.TEXT, received
                frame = json.loads(received.data)
                assert frame["frontend_id"] == frontend, frame
                if frame["message"].get("id") == request_id:
                    assert "result" in frame["message"], frame
                    return frame["message"]["result"]
                notes.append(frame["message"])

        session = (await request("session.open", {"scope": WORKSPACE}))["session"]
        assert session["scope"] == WORKSPACE, session
        sent = {"session_id": session["id"], "scope": WORKSPACE}
        sent["messages"] = [{"role": "user", "content": "Say something."}]
        accepted = await request("chat.send", sent)
        request_id = accepted["request_id"]
        assert accepted["accepted"] is True, accepted
        while not any(note["method"] == "chat.finished" for note in notes):
            frame = json.loads((await relay.receive()).data)
            notes.append(frame["message"])
        (finished,) = [note["params"] for note in notes if note["method"] == "chat.finished"]
        assert finished["status"] == "completed", finished
        chunks = [n["params"] for n in notes if n["method"] == "chat.chunk"]
        text = "".join(c["chunk"]["choices"][0]["delta"]["content"] for c in chunks)
        assert text == "".join(REPLY), notes
        history = await request("chat.history", {"session_id": session["id"], "scope": WORKSPACE})
        roles = [(i["message"]["role"], i["message"]["content"]) for i in history["items"]]
        assert ("user", "Say something.") in roles and ("assistant", "".join(REPLY)) in roles, roles
        print(
            "PASS AIDE session.open, chat.send, chunks, chat.finished and chat.history", flush=True
        )

        # Reads are authoritative: a new backend socket reads what the first one pushed.
        await relay.close()
        relay = await backend.get()
        target = {"session_id": session["id"], "request_id": request_id, "scope": WORKSPACE}
        read = await request("chat.events", target | {"after": {"attempt": 0, "seq": 0}})
        pushed = [n["params"]["event"] for n in notes if n["method"] == "chat.event"]
        assert read["events"] == pushed and read["done"] is True, (read, pushed)
        assert read["next"] == finished["cursor"], (read, finished)
        got = {"resource": "requests", "id": request_id, "scope": WORKSPACE}
        item = (await request("state.get", got))["item"]
        assert (item["status"], item["revision"]) == ("completed", finished["revision"]), item
    print(
        "PASS AIDE request record and chat.events read again after the socket is replaced",
        flush=True,
    )
    done.set()
    for runner in runners:
        await runner.cleanup()


asyncio.run(main())
