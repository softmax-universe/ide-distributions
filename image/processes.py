"""Reap owned subprocesses before the container supervisor exits."""

import asyncio
import socket
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def managed_process(
    *args: str, env: dict[str, str]
) -> AsyncIterator[asyncio.subprocess.Process]:
    child = await asyncio.create_subprocess_exec(*args, env=env)
    try:
        yield child
    finally:
        if child.returncode is None:
            child.terminate()
            finished = asyncio.create_task(child.wait())
            done, _ = await asyncio.wait([finished], timeout=10)
            if not done:
                child.kill()
            await finished


@asynccontextmanager
async def ready_process(
    *args: str, env: dict[str, str], timeout: float
) -> AsyncIterator[asyncio.subprocess.Process]:
    """Wait for this boot's owned listener to announce readiness, or fail on exit/timeout.

    The child gets systemd's ``NOTIFY_SOCKET``. Hermes's gateway sends ``READY=1`` and a
    ``STATUS=`` line on it once it runs (``gateway/systemd_notify.py``); ``research serve`` sends
    ``READY=1`` alone. Later datagrams (the gateway's ``WATCHDOG=1``) queue unread."""
    with tempfile.TemporaryDirectory(prefix="ready-") as directory:
        address = f"{directory}/notify.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
            receiver.bind(address)
            receiver.setblocking(False)
            async with managed_process(*args, env={**env, "NOTIFY_SOCKET": address}) as child:
                received = asyncio.create_task(asyncio.get_running_loop().sock_recv(receiver, 4096))
                exited = asyncio.create_task(child.wait())
                try:
                    async with asyncio.timeout(timeout):
                        done, _ = await asyncio.wait(
                            {received, exited}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if exited in done:
                            raise RuntimeError(
                                f"{args[0]} exited {exited.result()} before readiness"
                            )
                        if received.result().split(b"\n")[0] != b"READY=1":
                            raise RuntimeError(f"{args[0]} sent an invalid readiness notification")
                    yield child
                finally:
                    received.cancel()
                    exited.cancel()
                    await asyncio.gather(received, exited, return_exceptions=True)
