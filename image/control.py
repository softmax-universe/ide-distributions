"""Answer the platform's hosting control requests and translate them into the sleep command."""

import asyncio
import base64
import json
import logging
import time
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

log = logging.getLogger(__name__)


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    control: Literal["status", "sleep_request"]


class Lifecycle:
    def __init__(self, env: dict[str, str]):
        self.env = env
        self.sleep_task: asyncio.Task[int] | None = None
        self.last_busy = time.monotonic()

    async def prepare_sleep(self) -> int:
        process = await asyncio.create_subprocess_exec(
            "/opt/workspace-env/bin/research", "workspace", "sleep", env=self.env
        )
        return await process.wait()

    async def status(self, request: ControlRequest, seam: httpx.AsyncClient) -> dict:
        response = await seam.get("/status")
        response.raise_for_status()
        activity = response.json()
        if activity["restart_required"]:
            return {
                "ready": True,
                "ready_for_sleep": False,
                "detail": "Recovering native execution",
            }
        if activity["has_work"]:
            self.last_busy = time.monotonic()
        automatic = time.monotonic() - self.last_busy >= 900
        if (request.control == "sleep_request" or automatic) and (
            self.sleep_task is None or (self.sleep_task.done() and self.sleep_task.result() != 0)
        ):
            self.sleep_task = asyncio.create_task(self.prepare_sleep())
        sleeping = self.sleep_task is not None
        complete = sleeping and self.sleep_task.done()
        if sleeping and not complete:
            detail = "Preparing for sleep"
        elif activity["waiting_on"]:
            # Why the workspace stays awake, so hosting can see it; the platform caps detail.
            detail = ("Awake for " + ", ".join(activity["waiting_on"]))[:1024]
        else:
            detail = ""
        return {
            "ready": True,
            "ready_for_sleep": complete and self.sleep_task.result() == 0,
            "detail": detail,
        }


async def reply(
    socket: ClientConnection,
    stream_id: str,
    envelope: dict,
    lifecycle: Lifecycle,
    seam: httpx.AsyncClient,
) -> None:
    """Send one request's frames on the shared reply websocket (Metta spec 0150).

    Hosting control is the only request this connection carries; any other envelope is refused
    with 422 ``invalid``, as a malformed control request is."""

    async def validate() -> ControlRequest:
        return ControlRequest.model_validate(envelope)

    (request,) = await asyncio.gather(validate(), return_exceptions=True)
    if isinstance(request, ValidationError):
        result = httpx.Response(422, json={"kind": "invalid", "detail": str(request)})
    elif isinstance(request, BaseException):
        raise request
    else:
        result = httpx.Response(200, json=await lifecycle.status(request, seam))
    frame = {
        "request_id": str(UUID(envelope["id"])),
        "seq": 0,
        "stream_id": stream_id,
        "status": result.status_code,
        "content_type": "application/json",
        "data": "",
        "end": False,
    }
    await socket.send(json.dumps(frame))
    frame["stream_id"] = ""
    for offset in range(0, len(result.content), 65536):
        frame.update(
            seq=frame["seq"] + 1,
            data=base64.b64encode(result.content[offset : offset + 65536]).decode(),
        )
        await socket.send(json.dumps(frame))
    frame.update(seq=frame["seq"] + 1, data="", end=True)
    await socket.send(json.dumps(frame))


async def serve(
    *,
    platform_url: str,
    workspace_id: str,
    credential: str,
    env: dict[str, str],
    connected: asyncio.Event,
) -> None:
    """One bidirectional transport for hosting control; chat is the AIDE backend's (aide.py)."""
    lifecycle = Lifecycle(env)
    root = f"v2/workspaces/{workspace_id}"
    async with (
        httpx.AsyncClient(
            base_url=platform_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {credential}"},
        ) as player,
        httpx.AsyncClient(base_url="http://127.0.0.1:8650") as seam,
    ):
        url = player.build_request("GET", root + "/connection").url
        url = url.copy_with(scheme="wss" if url.scheme == "https" else "ws")

        async def answer(socket: ClientConnection, stream_id: str, envelope: dict) -> None:
            (result,) = await asyncio.gather(
                reply(socket, stream_id, envelope, lifecycle, seam),
                return_exceptions=True,
            )
            if isinstance(result, httpx.TransportError):
                (result,) = await asyncio.gather(
                    socket.send(
                        json.dumps(
                            {
                                "request_id": str(envelope["id"]),
                                "stream_id": stream_id,
                                "aborted": True,
                            }
                        )
                    ),
                    return_exceptions=True,
                )
            if isinstance(result, ConnectionClosed):
                log.info("Reply transport unavailable: %s", type(result).__name__)
            elif isinstance(result, BaseException):
                raise result

        async def session() -> None:
            async with connect(
                str(url), additional_headers={"Authorization": player.headers["Authorization"]}
            ) as socket:
                connected.set()
                running: dict[UUID, asyncio.Task[None]] = {}
                async with asyncio.TaskGroup() as replies:

                    async def receive() -> None:
                        async for message in socket:
                            incoming = json.loads(message)
                            if "envelope" not in incoming:
                                request_id = UUID(incoming["request_id"])
                                if request_id in running:
                                    running[request_id].cancel()
                                continue
                            request_id = UUID(incoming["envelope"]["id"])
                            if len(running) >= 32:
                                await socket.close(code=1013, reason="Too many in-flight requests")
                                return
                            task = replies.create_task(
                                answer(socket, incoming["stream_id"], incoming["envelope"])
                            )
                            running[request_id] = task
                            task.add_done_callback(lambda _, key=request_id: running.pop(key))

                    (closed,) = await asyncio.gather(receive(), return_exceptions=True)
                    for task in running.values():
                        task.cancel()
            if isinstance(closed, BaseException):
                raise closed

        while True:
            (result,) = await asyncio.gather(session(), return_exceptions=True)
            temporary = isinstance(result, ConnectionClosed | OSError | httpx.TransportError) or (
                isinstance(result, InvalidStatus) and result.response.status_code in {502, 503, 504}
            )
            if isinstance(result, BaseException) and not temporary:
                raise result
            await asyncio.sleep(1)
