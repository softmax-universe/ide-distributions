"""The shipped restricted profile on the pinned native gateway, with external HTTP fixtures."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from research_core import layout
from research_core.restricted_profile import INFERENCE_SENTINEL, prepare_restricted_profile
from research_core.serve import SeamServer
from research_core.store.actors import Actor
from research_core.store.db import Store
from research_core.watcher.hermes_api import HermesRuns
from tests.authority_service import authority_service
from tests.hermes.capture_rig import (
    API_KEY,
    STUB_USAGE,
    chunk,
    start_gateway,
    stream_reply,
    stub_server,
    whole_reply,
)
from tests.hermes.test_spike_s1 import free_port, hermes_python

REPO = Path(__file__).resolve().parents[3]


def test_restricted_native_profile_checks_authority_before_each_model_call(hermes_bin, tmp_path):
    accepted = []
    guarded_requests = []
    rejected = []
    rejected_bodies = []
    escaped = tmp_path / "native-tool-escaped"

    def answer(handler, raw, _):
        body = json.loads(raw)
        if handler.headers["Authorization"] not in {
            "Bearer restricted-model-fixture",
            "Bearer root-owner-model-fixture",
            "Bearer stub",
        }:
            rejected.append(handler.headers["Authorization"])
            rejected_bodies.append(body)
            handler.send_error(401)
            return
        accepted.append(body)
        if handler.headers["Authorization"] == "Bearer restricted-model-fixture":
            guarded_requests.append(body)
        if not body.get("stream"):
            whole_reply(handler, "Private reply", STUB_USAGE)
            return
        if body["messages"][-1]["content"] == "TRY_NATIVE_TOOL":
            stream_reply(
                handler,
                [
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "blocked-terminal",
                                    "type": "function",
                                    "function": {
                                        "name": "terminal",
                                        "arguments": json.dumps({"command": f"touch {escaped}"}),
                                    },
                                }
                            ]
                        }
                    ),
                    chunk({}, "tool_calls"),
                ],
            )
            return
        stream_reply(handler, [chunk({"content": "Private reply"}), chunk({}, "stop")])

    model = stub_server(answer)
    gateway = None
    seam = None
    with authority_service() as grants:
        try:
            gateway = start_gateway(
                tmp_path,
                hermes_python(hermes_bin),
                f"http://openrouter.ai:{model.server_port}/api/v1",
                free_port(),
            )
            profile = gateway.home / "profiles" / "restricted"
            escaped = profile / "conversation" / "native-tool-escaped"
            prepare_restricted_profile(
                profile,
                REPO,
                {
                    **grants.env,
                    "IDE_WORKSPACE": str(gateway.workspace),
                    "OPENROUTER_API_KEY": "restricted-model-fixture",
                    "API_SERVER_KEY": API_KEY,
                },
            )
            private_files = {
                gateway.home / "USER.md": "PRIVATE_ROOT_PROFILE_SENTINEL",
                gateway.home / "SOUL.md": "PRIVATE_ROOT_SOUL_SENTINEL",
                gateway.home / "AGENTS.md": "PRIVATE_ROOT_FILES_SENTINEL",
                gateway.home
                / "profiles"
                / "orchestrator"
                / "USER.md": "PRIVATE_OWNER_PROFILE_SENTINEL",
                gateway.workspace / "AGENTS.md": "PRIVATE_OWNER_FILES_SENTINEL",
            }
            for path, content in private_files.items():
                path.write_text(content)
            subprocess.run(["git", "init", "--quiet", str(gateway.home)], check=True)
            (gateway.home / "auth.json").write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "openrouter": [
                                {
                                    "id": "root-owner-key",
                                    "source": "manual",
                                    "access_token": "root-owner-model-fixture",
                                }
                            ]
                        }
                    }
                )
            )
            gateway.restart()
            gateway.run_turn("orchestrator", "api_owner_history", "PRIVATE_OWNER_HISTORY_SENTINEL")
            assert "PRIVATE_OWNER_HISTORY_SENTINEL" in json.dumps(accepted)
            accepted.clear()
            ws = layout.from_env({"IDE_WORKSPACE": str(gateway.workspace)})
            native = HermesRuns(
                base_url=f"http://127.0.0.1:{gateway.port}",
                keys={"orchestrator": API_KEY, "restricted": API_KEY},
            )
            seam = SeamServer(ws=ws, hermes=native, env=grants.env)
            authority = grants.issue()
            opened = seam.session_open(
                {
                    "op_id": str(uuid4()),
                    "client": {"id": "game-a", "kind": "test"},
                    "session_context": {"contract_version": 1, "scope": "workspace"},
                    "authority": authority.model_dump(mode="json"),
                }
            )
            session_id = opened["session_id"]
            op_id = str(uuid4())
            seam.message(
                {
                    "op_id": op_id,
                    "session_id": session_id,
                    "message": {"role": "user", "content": "PRIVATE_APPLICATION_MESSAGE"},
                    "authority": authority.model_dump(mode="json"),
                }
            )
            deadline = time.monotonic() + 60
            while True:
                asyncio.run(seam.dispatch_messages())
                with Store.open(ws.store_path, kind="workspace") as store:
                    admission = store.one("SELECT * FROM client_admissions WHERE op_id=?", (op_id,))
                if admission["status"] not in {"queued", "running"}:
                    break
                assert time.monotonic() < deadline, gateway.log_tail()
                time.sleep(0.05)
            assert admission["status"] == "completed", (dict(admission), gateway.log_tail())
            assert guarded_requests
            sent = json.dumps(guarded_requests)
            assert "PRIVATE_APPLICATION_MESSAGE" in sent
            for private in [*private_files.values(), "PRIVATE_OWNER_HISTORY_SENTINEL"]:
                assert private not in sent
            assert all(not request.get("tools") for request in guarded_requests)

            # Even an available native terminal must be denied at execution, not just hidden.
            gateway.stop()
            config = profile / "config.yaml"
            config.write_text(
                config.read_text().replace("api_server: []", "api_server: [terminal]")
            )
            gateway.restart()
            attack_id = str(uuid4())
            seam.message(
                {
                    "op_id": attack_id,
                    "session_id": session_id,
                    "message": {"role": "user", "content": "TRY_NATIVE_TOOL"},
                    "authority": authority.model_dump(mode="json"),
                }
            )
            deadline = time.monotonic() + 60
            while True:
                asyncio.run(seam.dispatch_messages())
                with Store.open(ws.store_path, kind="workspace") as store:
                    attack = store.one(
                        "SELECT status FROM client_admissions WHERE op_id=?", (attack_id,)
                    )
                if attack["status"] not in {"queued", "running"}:
                    break
                assert time.monotonic() < deadline, gateway.log_tail()
                time.sleep(0.05)
            assert not escaped.exists()

            # Exercise the native gate after durable dispatch, beyond the seam's admission check.
            revoked_session = seam.session_open(
                {
                    "op_id": str(uuid4()),
                    "client": {"id": "game-a", "kind": "test"},
                    "session_context": {"contract_version": 1, "scope": "workspace"},
                    "authority": authority.model_dump(mode="json"),
                }
            )["session_id"]
            second = str(uuid4())
            seam.message(
                {
                    "op_id": second,
                    "session_id": revoked_session,
                    "message": {"role": "user", "content": "REVOKED_MESSAGE"},
                    "authority": authority.model_dump(mode="json"),
                }
            )
            with (
                Store.open(ws.store_path, kind="workspace") as store,
                store.tx(Actor.user("test-native-dispatch")) as tx,
            ):
                tx.execute(
                    "UPDATE client_admissions SET dispatch_started=1 WHERE op_id=?", (second,)
                )
            grants.revoke(authority)
            before = len(accepted) + len(rejected)
            run_id = gateway.request(
                "POST",
                "restricted",
                "/v1/runs",
                {"input": "REVOKED_MESSAGE", "session_id": revoked_session},
            )["run_id"]
            deadline = time.monotonic() + 60
            while True:
                status = native.status(run_id, profile="restricted")
                if status.terminal:
                    break
                assert time.monotonic() < deadline, gateway.log_tail()
                time.sleep(0.05)
            assert len(accepted) + len(rejected) == before

            gateway.stop()
            config.write_text(
                config.read_text()
                .replace("enabled: [restricted]", "enabled: []")
                .replace("api_server: [terminal]", "api_server: []")
            )
            gateway.restart()
            before = len(accepted)
            run_id = gateway.request(
                "POST",
                "restricted",
                "/v1/runs",
                {"input": "PLUGIN_MISSING", "session_id": "missing-plugin"},
            )["run_id"]
            deadline = time.monotonic() + 60
            while not native.status(run_id, profile="restricted").terminal:
                assert time.monotonic() < deadline, gateway.log_tail()
                time.sleep(0.05)
            assert len(accepted) == before
            assert rejected, "The unguarded profile must carry only an unusable provider credential"
            assert set(rejected) == {f"Bearer {INFERENCE_SENTINEL}"}
            for private in [*private_files.values(), "PRIVATE_OWNER_HISTORY_SENTINEL"]:
                assert private not in json.dumps(rejected_bodies)
        finally:
            if seam is not None:
                seam._http.server_close()
            if gateway is not None:
                gateway.stop()
            model.shutdown()
            model.server_close()
