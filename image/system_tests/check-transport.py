"""Exercise control connection failures over real HTTP and websocket connections."""

import asyncio
import base64
import json
import sys
from pathlib import Path
from uuid import uuid4

from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control import serve as bridge  # noqa: E402


async def main():
    held_started = asyncio.Event()
    release_held = asyncio.Event()
    finished = asyncio.Event()
    statuses = 0
    connections = 0
    connected = asyncio.Event()

    async def seam(reader, writer):
        nonlocal statuses
        headers = await reader.readuntil(b"\r\n\r\n")
        assert headers.split(b" ")[1] == b"/status", headers
        statuses += 1
        index = statuses
        if index == 1:
            held_started.set()
            await release_held.wait()
        if index != 2:
            body = json.dumps({"restart_required": False, "has_work": True, "waiting_on": []})
            writer.write(
                (
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"
                ).encode()
            )
            await writer.drain()
        # The second request closes before headers: a transport failure, not an application one.
        writer.close()
        await writer.wait_closed()

    async def send(socket, envelope, stream_id):
        await socket.send(json.dumps({"stream_id": stream_id, "envelope": envelope}))
        return envelope["id"]

    def control(kind="status"):
        return {"id": str(uuid4()), "control": kind}

    async def reply(socket, request_id):
        frames = [json.loads(await socket.recv())]
        while not frames[-1]["end"]:
            frames.append(json.loads(await socket.recv()))
        assert all(frame["request_id"] == request_id for frame in frames)
        assert [frame["seq"] for frame in frames] == list(range(len(frames)))
        body = json.loads(b"".join(base64.b64decode(frame["data"]) for frame in frames))
        return frames[0]["status"], body

    async def platform(socket):
        nonlocal connections
        connections += 1
        held = await send(socket, control(), "1-0")
        await held_started.wait()
        unavailable = await send(socket, control(), "2-0")
        assert json.loads(await socket.recv()) == {
            "request_id": unavailable,
            "stream_id": "2-0",
            "aborted": True,
        }
        # Only hosting control travels here: any other envelope gets the ordinary refusal.
        other = {"id": str(uuid4()), "op_id": str(uuid4()), "name": "anything", "args": {}}
        status, body = await reply(socket, await send(socket, other, "3-0"))
        assert status == 422 and body["kind"] == "invalid", body
        release_held.set()
        status, body = await reply(socket, held)
        assert status == 200 and body["ready"] and not body["ready_for_sleep"], body
        status, body = await reply(socket, await send(socket, control(), "4-0"))
        assert status == 200 and body["ready"], body
        finished.set()
        await socket.wait_closed()

    async with (
        await asyncio.start_server(seam, "127.0.0.1", 8650),
        serve(platform, "127.0.0.1", 0) as server,
    ):
        port = server.sockets[0].getsockname()[1]
        task = asyncio.create_task(
            bridge(
                platform_url=f"http://127.0.0.1:{port}",
                workspace_id=str(uuid4()),
                credential="proof",
                env={},
                connected=connected,
            )
        )
        try:
            async with asyncio.timeout(10):
                await finished.wait()
            assert connected.is_set()
            assert connections == 1 and not task.done(), (
                "A failed request restarted the shared connection"
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    print("PASS control aborts and refusals preserve concurrent replies and the shared websocket")


asyncio.run(main())
