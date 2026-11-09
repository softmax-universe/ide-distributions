"""Exercise the packaged AIDE backend against the running seam, without model credentials."""

import asyncio
import json
import os
import shutil
import signal
import sqlite3
import sys
import time
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aide import Backend, History, Open, Refusal, Send, StateList  # noqa: E402
from inference import Availability  # noqa: E402

WORKSPACE = {"scope": "workspace", "coworld": None, "league": None}
FRONTEND = uuid4()


async def main():
    assert shutil.which("pdftoppm"), "The packaged native PDF attachment renderer is missing"
    assert version("faster-whisper") == "1.2.1"
    assert Path("/opt/models/whisper-base/model.bin").is_file()
    for role in ("orchestrator", "researcher"):
        config = yaml.safe_load(Path(f"/workspace/host/profiles/{role}/config.yaml").read_text())
        assert config["stt"]["local"]["model"] == "/opt/models/whisper-base"
    async with (
        httpx.AsyncClient(base_url="http://127.0.0.1:8650") as seam,
        httpx.AsyncClient(
            base_url="http://127.0.0.1:8642/p/orchestrator/",
            headers={
                "Authorization": "Bearer "
                + Path("/workspace/host/.gateway-key").read_text().strip()
            },
        ) as gateway,
        asyncio.TaskGroup() as work,
    ):
        # The fixture has no inference lease, so model work cannot start.
        backend = Backend(seam, gateway, Availability(), work)
        opening = Open.model_validate(
            {"scope": WORKSPACE, "client": {"id": "intake-proof", "kind": "test"}}
        )

        async def open_session(request_id: str) -> str:
            return (await backend.open(FRONTEND, request_id, opening))["session"]["id"]

        async def history(session_id: str) -> list:
            params = History(session_id=session_id, scope=WORKSPACE)
            return (await backend.history(FRONTEND, str(uuid4()), params))["items"]

        opened = "00000000-0000-4000-8000-000000000124"
        session = await open_session(opened)
        assert await history(session) == []
        # Recover the native receipt, including the second retained-volume cycle.
        assert await open_session(opened) == session
        saved = Path("/workspace/.intake-session")
        if saved.exists():
            assert saved.read_text() == session
        else:
            saved.write_text(session)
        # Stop the actual pinned Hermes process, without replacing any runtime service.
        gateways = [
            int(process.name)
            for process in Path("/proc").iterdir()
            if process.name.isdigit()
            and (process / "cmdline").exists()
            and (process / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")[-2:]
            == [b"-m", b"gateway.run"]
        ]
        assert len(gateways) == 1, gateways
        os.kill(gateways[0], signal.SIGSTOP)
        pending = []
        try:
            for _ in range(2):
                started = time.monotonic()
                waiting = await open_session(str(uuid4()))
                assert time.monotonic() - started < 1, "Opening waited for Hermes"
                pending.append(waiting)
                # Let the serial dispatcher enter its blocked native call.
                await asyncio.sleep(0.6)
                opening_state = await seam.get("/session-pending", params={"session_id": waiting})
                assert opening_state.json() == {"pending": True}
            body = {
                "op_id": str(uuid4()),
                "session_id": waiting,
                "message": {"role": "user", "content": "Durable intake fault proof"},
            }
            started = time.monotonic()
            accepted = await seam.post("/message", json=body, timeout=1)
            assert accepted.status_code == 200 and accepted.json()["delivered"] == "queued"
            assert time.monotonic() - started < 1, "Message intake waited for Hermes"
            with sqlite3.connect(
                "file:/workspace/research/.state/store.db?mode=ro", uri=True
            ) as store:
                request, status = store.execute(
                    "SELECT request,status FROM client_admissions WHERE op_id=?", (body["op_id"],)
                ).fetchone()
                assert json.loads(request) == body and status == "queued"
            started = time.monotonic()
            assert await history(waiting) == []
            assert time.monotonic() - started < 1, "Pending history waited for Hermes"
        finally:
            os.kill(gateways[0], signal.SIGCONT)
        deadline = time.monotonic() + 10
        while pending and time.monotonic() < deadline:
            for waiting in pending[:]:
                ready = await seam.get("/session-pending", params={"session_id": waiting})
                if not ready.json()["pending"]:
                    pending.remove(waiting)
            await asyncio.sleep(0.1)
        assert not pending, "Native openings did not recover after resuming Hermes"
        # This proves intake, not model dispatch: the fixture has no inference lease.
        cancelled = await seam.post("/message-cancel", json={"op_id": body["op_id"]})
        assert cancelled.status_code == 200, cancelled.text
        assert await history(waiting) == []
        send = Send.model_validate(
            {
                "session_id": session,
                "messages": [{"role": "user", "content": "intake proof"}],
                "scope": WORKSPACE,
                "context": {},
            }
        )
        refusal = None
        try:
            await backend.send_chat(FRONTEND, str(uuid4()), send)
        except Refusal as refused:
            refusal = refused
        assert refusal is not None and refusal.code == "inference_unavailable", refusal
        listing = await backend.list_state(FRONTEND, str(uuid4()), StateList(resource="sessions"))
        assert session in [item["id"] for item in listing["items"]], listing
    print("PASS AIDE backend: durable intake and empty history while native Hermes is stopped;")
    print("  native recovery; open receipt/replay; session listing; inference gate")


asyncio.run(main())
