"""The lease loop follows the platform's delays, publishes credentials before readiness, and
keeps the inference state: a spent key blocks model work until a new key arrives."""

import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inference import Availability, maintain, record_state  # noqa: E402

from research_core import inference as state  # noqa: E402  (on the path through inference)
from research_core.plugin.inference_hook import (  # noqa: E402
    KEY_CHANGE_GRACE,
    record_api_error,
)


def lease(key: str, expires: datetime) -> tuple[str, str, bytes]:
    body = {
        "key": key,
        "expires_at": expires.isoformat(),
        # Renew at once: the loop's two-second floor paces the scenario.
        "renew_at": datetime.now(UTC).isoformat(),
        "allowance_usd": 1,
    }
    return "200 OK", "", json.dumps(body).encode()


async def main(tmp_path: Path):
    requests = []
    expires = datetime.now(UTC) + timedelta(hours=1)
    refill = expires - state.RENEWAL_GRACE
    homes = (tmp_path / "host", tmp_path / "profile")
    for home in homes:
        home.mkdir()
        (home / ".env").write_text("PRESERVED=original\nOPENROUTER_API_KEY=expired\n")
    marker = tmp_path / "lease"
    availability = Availability(marker)
    ready = asyncio.Event()
    done = asyncio.Event()

    installed = None

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        request = await reader.readuntil(b"\r\n\r\n")
        assert request.startswith(b"PUT /v2/workspaces/proof/inference HTTP/1.1\r\n")
        assert b"Authorization: Bearer fixture" in request
        nonlocal installed
        requests.append(time.monotonic())
        if len(requests) == 1:
            assert not ready.is_set() and not availability.available() and not marker.exists()
            # No lease yet: the gate is closed and names no reason or refill time.
            assert availability.blocking() is None
            status, headers, body = "503 Service Unavailable", "Retry-After: 1\r\n", b"{}"
        elif len(requests) == 2:
            status, headers, body = lease("fixture-key", expires)
        elif len(requests) == 3:
            # The first lease is installed; now the key runs dry mid-run.
            assert availability.available()
            installed = state.read_lease(marker)
            assert installed.key_id == state.key_id("fixture-key")
            # Later than the hook's grace after a key change, so the error counts.
            record_api_error(
                marker,
                session_id="api_proof",
                reason="billing",
                status_code=402,
                now=datetime.now(UTC) + KEY_CHANGE_GRACE,
            )
            assert not availability.available()
            # The platform hands back the same, spent key: that must not end the block, nor
            # move the key's change time.
            status, headers, body = lease("fixture-key", expires)
        elif len(requests) == 4:
            assert state.read_state(marker).blocked == "budget_exhausted"
            assert state.read_lease(marker).key_changed_at == installed.key_changed_at
            assert not availability.available()
            status, headers = "200 OK", ""
            body = json.dumps(
                {
                    "kind": "budget_exhausted",
                    "message": "spent",
                    "next_refill_at": refill.isoformat(),
                    "retry_at": datetime.now(UTC).isoformat(),
                }
            ).encode()
        else:
            blocking = availability.blocking()
            assert not availability.available() and blocking.blocked == "budget_exhausted"
            assert blocking.until == refill
            assert state.read_state(marker).last_error.session_id == "api_proof"
            status, headers, body = lease("fresh-key", expires + timedelta(days=1))
            done.set()
        writer.write(
            (
                f"HTTP/1.1 {status}\r\n{headers}Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        task = asyncio.create_task(
            maintain(
                platform_url=f"http://127.0.0.1:{port}",
                workspace_id="proof",
                credential="fixture",
                homes=homes,
                availability=availability,
                lease_file=marker,
                ready=ready,
            )
        )
        try:
            async with asyncio.timeout(5):
                await ready.wait()
            assert len(requests) == 2
            assert requests[1] - requests[0] >= 1
            print(f"Inference retry interval: {requests[1] - requests[0]:.3f}s")
            assert availability.available()
            assert state.read_lease(marker).expires_at == expires
            for home in homes:
                assert dotenv_values(home / ".env") == {
                    "PRESERVED": "original",
                    "OPENROUTER_API_KEY": "fixture-key",
                }
            async with asyncio.timeout(15):
                await done.wait()
                while not availability.available():
                    await asyncio.sleep(0.05)
            # The new key alone ended the block: the state still names the spent key.
            assert state.read_lease(marker).key_id == state.key_id("fresh-key")
            assert state.read_state(marker).blocked_key_id == state.key_id("fixture-key")
            for home in homes:
                assert dotenv_values(home / ".env")["OPENROUTER_API_KEY"] == "fresh-key"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def unwritable_state(tmp_path: Path):
    """A state write that fails is logged, not raised: it must not stop the workspace."""
    (tmp_path / "a-file").write_text("")
    lease_file = tmp_path / "a-file" / "lease"
    await record_state(state.lease_refused, lease_file, datetime.now(UTC), budget_exhausted=True)


with TemporaryDirectory() as directory:
    asyncio.run(main(Path(directory)))
with TemporaryDirectory() as directory:
    asyncio.run(unwritable_state(Path(directory)))
print(
    "PASS inference retry follows server delay and publishes credentials before readiness;"
    " a spent key blocks until a new key arrives"
)
